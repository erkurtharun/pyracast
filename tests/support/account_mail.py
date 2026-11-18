import json
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import product
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from allauth.account.models import EmailAddress
from allauth.core.internal import ratelimit
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal import auth as totp_auth
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import Client
from django.utils import dateformat, translation
from django.utils.html import escape

from pyracast.accounts.models import UserLanguagePreference
from tests.support.auth_lifecycle import AUTH, PASSWORD, submit, totp_code
from tests.support.company_registration import signup
from tests.support.social_accounts import CALLBACK, PROVIDERS, complete, provider_destination

FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0"
HOSTILE_AGENT = f'{FIREFOX} <script>alert("agent")</script>\r\nX-Injected: agent'
CODE = re.compile(r"^\s*([A-Z0-9]{4}-[A-Z0-9]{4}|[0-9]{6})\s*$", re.MULTILINE)
OTHER = {"en": "tr", "tr": "en"}
CODE_COPY = {
    "en": (
        "Enter this code in the Pyracast window where you requested it.",
        "This code expires in {minutes} minutes. Do not share it.",
    ),
    "tr": (
        "Bu kodu, isteği başlatt\u0131ğ\u0131n\u0131z Pyracast penceresine girin.",
        "Bu kod {minutes} dakika içinde sona erer. Kodu paylaşmay\u0131n.",
    ),
}
CODE_SUBJECTS = {
    "en": {
        "account/email/email_confirmation_signup": "Verify your email",
        "account/email/email_confirmation": "Verify your email",
        "account/email/password_reset_code": "Reset your password",
        "account/email/login_code": "Confirm your identity",
    },
    "tr": {
        "account/email/email_confirmation_signup": "E-posta adresinizi doğrulay\u0131n",
        "account/email/email_confirmation": "E-posta adresinizi doğrulay\u0131n",
        "account/email/password_reset_code": "Parolan\u0131z\u0131 s\u0131f\u0131rlay\u0131n",
        "account/email/login_code": "Kimliğinizi doğrulay\u0131n",
    },
}
NOTICE_COPY = {
    "en": {
        "account/email/password_changed": (
            "Your password was changed",
            "The password of your Pyracast account was changed.",
        ),
        "account/email/password_set": (
            "A password was set for your account",
            "A password was set for your Pyracast account.",
        ),
        "account/email/password_reset": (
            "Your password was reset",
            "The password of your Pyracast account was reset.",
        ),
        "account/email/email_changed": (
            "Your email address was changed",
            "Your Pyracast email address changed from {from_email} to {to_email}.",
        ),
        "account/email/email_deleted": (
            "An email address was removed",
            "{deleted_email} was removed from your Pyracast account.",
        ),
        "mfa/email/totp_activated": (
            "Two-factor authentication was turned on",
            "Two-factor authentication with an authenticator app was turned on for your account.",
        ),
        "mfa/email/totp_deactivated": (
            "Two-factor authentication was turned off",
            "Two-factor authentication with an authenticator app was turned off for your account.",
        ),
        "mfa/email/recovery_codes_generated": (
            "New recovery codes were generated",
            "New recovery codes were generated for your account. "
            "The previous codes no longer work.",
        ),
        "socialaccount/email/account_connected": (
            "A sign-in provider was connected",
            "{provider} was connected to your Pyracast account for signing in.",
        ),
        "socialaccount/email/account_disconnected": (
            "A sign-in provider was disconnected",
            "{provider} was disconnected from your Pyracast account.",
        ),
        "account/email/unknown_account": (
            "Pyracast account not found",
            "Someone tried to use {email} with Pyracast, but no account uses this address.",
        ),
        "account/email/account_already_exists": (
            "Your Pyracast account already exists",
            "Someone tried to create a Pyracast account with {email}, which already has one.",
        ),
    },
    "tr": {
        "account/email/password_changed": (
            "Parolan\u0131z değiştirildi",
            "Pyracast hesab\u0131n\u0131z\u0131n parolas\u0131 değiştirildi.",
        ),
        "account/email/password_set": (
            "Hesab\u0131n\u0131z için parola belirlendi",
            "Pyracast hesab\u0131n\u0131z için bir parola belirlendi.",
        ),
        "account/email/password_reset": (
            "Parolan\u0131z s\u0131f\u0131rland\u0131",
            "Pyracast hesab\u0131n\u0131z\u0131n parolas\u0131 s\u0131f\u0131rland\u0131.",
        ),
        "account/email/email_changed": (
            "E-posta adresiniz değiştirildi",
            "Pyracast e-posta adresiniz {from_email} yerine {to_email} olarak değişti.",
        ),
        "account/email/email_deleted": (
            "Bir e-posta adresi kald\u0131r\u0131ld\u0131",
            "{deleted_email}, Pyracast hesab\u0131n\u0131zdan kald\u0131r\u0131ld\u0131.",
        ),
        "mfa/email/totp_activated": (
            "\u0130ki ad\u0131ml\u0131 doğrulama aç\u0131ld\u0131",
            "Hesab\u0131n\u0131z için doğrulama uygulamas\u0131yla "
            "iki ad\u0131ml\u0131 doğrulama aç\u0131ld\u0131.",
        ),
        "mfa/email/totp_deactivated": (
            "\u0130ki ad\u0131ml\u0131 doğrulama kapat\u0131ld\u0131",
            "Hesab\u0131n\u0131z için doğrulama uygulamas\u0131yla "
            "iki ad\u0131ml\u0131 doğrulama kapat\u0131ld\u0131.",
        ),
        "mfa/email/recovery_codes_generated": (
            "Yeni kurtarma kodlar\u0131 oluşturuldu",
            "Hesab\u0131n\u0131z için yeni kurtarma kodlar\u0131 oluşturuldu. "
            "Önceki kodlar art\u0131k geçerli değil.",
        ),
        "socialaccount/email/account_connected": (
            "Bir oturum açma sağlay\u0131c\u0131s\u0131 bağland\u0131",
            "{provider}, oturum açmak için Pyracast hesab\u0131n\u0131za bağland\u0131.",
        ),
        "socialaccount/email/account_disconnected": (
            "Bir oturum açma sağlay\u0131c\u0131s\u0131n\u0131n "
            "bağlant\u0131s\u0131 kald\u0131r\u0131ld\u0131",
            "{provider} bağlant\u0131s\u0131 "
            "Pyracast hesab\u0131n\u0131zdan kald\u0131r\u0131ld\u0131.",
        ),
        "account/email/unknown_account": (
            "Pyracast hesab\u0131 bulunamad\u0131",
            "Birisi {email} adresini Pyracast'te kullanmay\u0131 denedi, "
            "ancak bu adresi kullanan bir hesap yok.",
        ),
        "account/email/account_already_exists": (
            "Pyracast hesab\u0131n\u0131z zaten var",
            "Birisi {email} adresiyle Pyracast hesab\u0131 oluşturmay\u0131 denedi, "
            "ancak bu adresin zaten bir hesab\u0131 var.",
        ),
    },
}
ACTION_LABELS = {
    "en": {
        "account/email/unknown_account": "Create an account",
        "account/email/account_already_exists": "Reset password",
    },
    "tr": {
        "account/email/unknown_account": "Hesap oluştur",
        "account/email/account_already_exists": "Parolay\u0131 s\u0131f\u0131rla",
    },
}
SECURITY_COPY = {
    "en": {
        "time": "Time",
        "device": "Device: Firefox on Windows",
        "ip": "IP address: 127.0.0.1",
        "warning": "If you did not make this change, secure your account now:",
        "link": "Secure your account",
    },
    "tr": {
        "time": "Zaman",
        "device": "Cihaz: Windows üzerinde Firefox",
        "ip": "IP adresi: 127.0.0.1",
        "warning": (
            "Bu değişikliği siz yapmad\u0131ysan\u0131z hesab\u0131n\u0131z\u0131 "
            "hemen güvenceye al\u0131n:"
        ),
        "link": "Hesab\u0131n\u0131z\u0131 güvenceye al\u0131n",
    },
}


SIGNUP_CODE = "account/email/email_confirmation_signup"
CHANGE_CODE = "account/email/email_confirmation"
RESET_CODE = "account/email/password_reset_code"
LOGIN_CODE = "account/email/login_code"
EXISTS = "account/email/account_already_exists"
UNKNOWN_ACCOUNT = "account/email/unknown_account"
RESET_NOTICE = "account/email/password_reset"
CHANGED_NOTICE = "account/email/password_changed"
SET_NOTICE = "account/email/password_set"
EMAIL_CHANGED = "account/email/email_changed"
EMAIL_DELETED = "account/email/email_deleted"
TOTP_ON = "mfa/email/totp_activated"
TOTP_OFF = "mfa/email/totp_deactivated"
RECOVERY_CODES = "mfa/email/recovery_codes_generated"
CONNECTED = "socialaccount/email/account_connected"
DISCONNECTED = "socialaccount/email/account_disconnected"
CODE_MINUTES = {SIGNUP_CODE: 10, CHANGE_CODE: 10, RESET_CODE: 10, LOGIN_CODE: 3}
CONFIRMATIONS = {
    SIGNUP_CODE: ("auth/email/verify", "key"),
    CHANGE_CODE: ("auth/email/verify", "key"),
    LOGIN_CODE: ("auth/reauthenticate/code/confirm", "code"),
}
ACTION_URLS = {
    EXISTS: settings.HEADLESS_FRONTEND_URLS["account_reset_password"],
    UNKNOWN_ACCOUNT: settings.HEADLESS_FRONTEND_URLS["account_signup"],
}
PROVIDER_NAMES = {"google": "Google", "microsoft": "Microsoft"}
OTHER_PROVIDER = {"google": "microsoft", "microsoft": "google", "none": "google"}
REFUSAL = {
    "en": "This email address is already verified.",
    "tr": "Bu e-posta adresi zaten doğrulanm\u0131ş.",
}

PRIMARY = "member@example.com"
SECONDARY = "second@example.com"
UNVERIFIED = "pending@example.com"
OTHER_ADDRESS = "other@example.com"
UNKNOWN = "missing@example.com"
INACTIVE_ADDRESS = "inactive@example.com"
FRESH = "fresh@example.com"
CHANGE = "change@example.com"

LANGUAGES = ("en", "tr")
SHAPES = (
    "verified",
    "verified-secondary",
    "unverified-secondary",
    "unverified-new",
    "unverified-returning",
    "inactive",
    "passwordless",
    "passwordless-linked",
)
ADDRESSES = {
    "own-primary": PRIMARY,
    "own-verified-secondary": SECONDARY,
    "own-unverified-secondary": UNVERIFIED,
    "other-account": OTHER_ADDRESS,
    "unknown": UNKNOWN,
    "inactive-account": INACTIVE_ADDRESS,
}
TARGETS = (*ADDRESSES, "pending-address", "none")
STATES = (
    "signed-out",
    "pending-signup",
    "pending-provider-signup",
    "signed-in",
    "reauthentication-pending",
    "reauthenticated",
    "pending-change",
    "pending-stored-change",
)
PROVIDER_CHOICES = ("none", "google", "microsoft")
ANONYMOUS = frozenset({"signed-out", "pending-signup", "pending-provider-signup"})
SIGNED = frozenset(STATES) - ANONYMOUS
NOT_REAUTHENTICATED = frozenset({"signed-in", "reauthentication-pending"})
PENDING_CHANGES = frozenset({"pending-change", "pending-stored-change"})
PENDING_VERIFICATION = PENDING_CHANGES | {"pending-signup"}
NO_VERIFICATION = frozenset(STATES) - PENDING_VERIFICATION
OWN = frozenset({"own-primary", "own-verified-secondary", "own-unverified-secondary"})
ELSEWHERE = frozenset({"other-account", "unknown", "inactive-account"})
UNVERIFIED_SHAPES = frozenset({"unverified-new", "unverified-returning"})
PASSWORDLESS = frozenset({"passwordless", "passwordless-linked"})
SESSION_SHAPES = PASSWORDLESS | {"verified", "verified-secondary", "unverified-secondary"}


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    takes_address: bool
    provider: bool = False
    mfa: bool = False

    def route(self, provider: str) -> str:
        return (
            self.path.format(provider=provider) if self.path.startswith("/") else AUTH + self.path
        )


CALLBACK_ROUTE = "/api/accounts/{provider}/login/callback/"
ENDPOINTS = {
    "signup": Endpoint("POST", "auth/signup", True),
    "login": Endpoint("POST", "auth/login", True),
    "reset-request": Endpoint("POST", "auth/password/request", True),
    "reset": Endpoint("POST", "auth/password/reset", True),
    "verify": Endpoint("POST", "auth/email/verify", False),
    "resend": Endpoint("POST", "auth/email/verify/resend", False),
    "add-address": Endpoint("POST", "account/email", True),
    "select-address": Endpoint("PUT", "account/email", True),
    "make-primary": Endpoint("PATCH", "account/email", True),
    "remove-address": Endpoint("DELETE", "account/email", True),
    "password": Endpoint("POST", "account/password/change", False),
    "reauthentication-code": Endpoint("POST", "auth/reauthenticate/code/request", True),
    "totp-on": Endpoint("POST", "account/authenticators/totp", False),
    "totp-off": Endpoint("DELETE", "account/authenticators/totp", False, mfa=True),
    "recovery-codes": Endpoint("POST", "account/authenticators/recovery-codes", False, mfa=True),
    "disconnect": Endpoint("DELETE", "account/providers", False, provider=True),
    "provider-signup": Endpoint("POST", "auth/provider/signup", True, provider=True),
    "provider-login": Endpoint("GET", CALLBACK_ROUTE, True, provider=True),
    "provider-connect": Endpoint("GET", CALLBACK_ROUTE, False, provider=True),
}
GATED = {
    "signup": SIGNED,
    "login": SIGNED,
    "verify": NO_VERIFICATION,
    "resend": NO_VERIFICATION,
    "provider-connect": ANONYMOUS | NOT_REAUTHENTICATED,
} | {
    name: ANONYMOUS
    for name in (
        "add-address",
        "select-address",
        "make-primary",
        "remove-address",
        "password",
        "reauthentication-code",
        "totp-on",
        "totp-off",
        "recovery-codes",
        "disconnect",
    )
}


@dataclass(frozen=True)
class Combination:
    endpoint: str
    shape: str
    target: str
    state: str
    provider: str


@dataclass(frozen=True)
class Mail:
    template: str
    to: str
    language: str
    status: int


@dataclass(frozen=True)
class NoMail:
    status: int
    reason: str


@dataclass(frozen=True)
class Expect:
    endpoint: str
    outcome: Mail | NoMail
    shapes: frozenset[str] | None = None
    targets: frozenset[str] | None = None
    states: frozenset[str] | None = None
    providers: frozenset[str] | None = None

    def matches(self, combination: Combination) -> bool:
        return (
            self.endpoint == combination.endpoint
            and (self.shapes is None or combination.shape in self.shapes)
            and (self.targets is None or combination.target in self.targets)
            and (self.states is None or combination.state in self.states)
            and (self.providers is None or combination.provider in self.providers)
        )


def expect(
    endpoint: str,
    outcome: Mail | NoMail,
    *,
    shapes: Iterable[str] | None = None,
    targets: Iterable[str] | None = None,
    states: Iterable[str] | None = None,
    providers: Iterable[str] | None = None,
) -> Expect:
    return Expect(
        endpoint,
        outcome,
        None if shapes is None else frozenset(shapes),
        None if targets is None else frozenset(targets),
        None if states is None else frozenset(states),
        None if providers is None else frozenset(providers),
    )


def gated(combination: Combination) -> bool:
    return combination.state in GATED.get(combination.endpoint, frozenset())


def canonical_target(endpoint: str) -> str:
    return "own-primary" if ENDPOINTS[endpoint].takes_address else "none"


def touches_account(combination: Combination) -> bool:
    return combination.target in OWN or (
        combination.endpoint in {"verify", "resend"}
        and combination.state == "pending-signup"
        and combination.shape in UNVERIFIED_SHAPES
    )


NOT_APPLICABLE: tuple[tuple[str, Callable[[Combination], bool]], ...] = (
    (
        "only endpoints that read an address in the request take a target",
        lambda c: (c.target == "none") == ENDPOINTS[c.endpoint].takes_address,
    ),
    (
        "only provider endpoints take a provider",
        lambda c: (c.provider == "none") == ENDPOINTS[c.endpoint].provider,
    ),
    (
        "an own secondary target exists only in the account shape that has it",
        lambda c: (
            (c.target == "own-verified-secondary" and c.shape != "verified-secondary")
            or (c.target == "own-unverified-secondary" and c.shape != "unverified-secondary")
        ),
    ),
    (
        "the pending address exists only while a change is pending",
        lambda c: c.target == "pending-address" and c.state not in PENDING_CHANGES,
    ),
    (
        "while a change started by PUT is pending, the stored secondary is the pending address",
        lambda c: c.state == "pending-stored-change" and c.target == "own-unverified-secondary",
    ),
    (
        "a change started by PUT verifies the stored unverified secondary, which only that "
        "account shape has",
        lambda c: c.state == "pending-stored-change" and c.shape != "unverified-secondary",
    ),
    (
        "a provider sign-in names the account its identity is linked to, not one of its addresses",
        lambda c: (
            c.endpoint == "provider-login"
            and c.target
            in {"own-verified-secondary", "own-unverified-secondary", "pending-address"}
        ),
    ),
    (
        "only an account that can sign in holds a signed-in session",
        lambda c: c.state in SIGNED and c.shape not in SESSION_SHAPES,
    ),
    (
        "only password change and provider disconnect decide on mail by whether the account has "
        "a password; sign-in and password reauthentication read it but never send mail",
        lambda c: c.shape in PASSWORDLESS and c.endpoint not in {"password", "disconnect"},
    ),
    (
        "signed out, the account's shape matters only when the request touches the account",
        lambda c: c.state in ANONYMOUS and c.shape != "verified" and not touches_account(c),
    ),
    (
        "a reset completes only with the code its request mailed",
        lambda c: (
            c.endpoint == "reset"
            and (
                c.target in {"own-unverified-secondary", "unknown", "inactive-account"}
                or c.target == "pending-address"
                or (c.target == "own-primary" and c.shape == "inactive")
            )
        ),
    ),
    (
        "a state rejected before the body is read runs once per state and provider",
        lambda c: gated(c) and (c.shape != "verified" or c.target != canonical_target(c.endpoint)),
    ),
)
LANGUAGE_RULE = (
    "a sending row runs its first combination per provider in both languages and every further "
    "one in one language, alternating by position; a combination without mail has nothing to "
    "localize and runs in English"
)


def notifies(endpoint: str, template: str) -> tuple[Expect, ...]:
    return (
        expect(endpoint, NoMail(401, "no session"), states=ANONYMOUS),
        expect(endpoint, NoMail(401, "needs reauthentication"), states=NOT_REAUTHENTICATED),
        expect(endpoint, Mail(template, "primary", "member", 200)),
    )


EXPECTED: tuple[Expect, ...] = (
    expect("signup", NoMail(409, "a signed-in client cannot sign up"), states=SIGNED),
    expect("signup", Mail(SIGNUP_CODE, "target", "request", 401), targets={"unknown"}),
    expect(
        "signup",
        Mail(EXISTS, "target", "request", 401),
        targets={"own-primary"},
        shapes=UNVERIFIED_SHAPES,
    ),
    expect("signup", Mail(EXISTS, "target", "request", 401), targets={"own-unverified-secondary"}),
    expect(
        "signup",
        Mail(EXISTS, "target", "member", 401),
        targets={"own-primary", "own-verified-secondary"},
    ),
    expect("signup", Mail(EXISTS, "target", "other", 401), targets={"other-account"}),
    expect("signup", Mail(EXISTS, "target", "inactive", 401), targets={"inactive-account"}),
    expect("login", NoMail(409, "already signed in"), states=SIGNED),
    expect(
        "login",
        Mail(SIGNUP_CODE, "target", "member", 401),
        targets={"own-primary"},
        shapes={"unverified-new"},
    ),
    expect(
        "login",
        Mail(CHANGE_CODE, "target", "member", 401),
        targets={"own-primary"},
        shapes={"unverified-returning"},
    ),
    expect(
        "login",
        NoMail(400, "an inactive account is refused like a wrong password"),
        targets={"own-primary"},
        shapes={"inactive"},
    ),
    expect(
        "login",
        NoMail(200, "signs in"),
        targets={"own-primary", "own-verified-secondary", "other-account"},
    ),
    expect("login", NoMail(400, "not a sign-in identity"), targets={"own-unverified-secondary"}),
    expect("login", NoMail(400, "not a sign-in identity"), targets={"unknown"}),
    expect(
        "login",
        NoMail(400, "an inactive account is refused like a wrong password"),
        targets={"inactive-account"},
    ),
    expect(
        "reset-request",
        Mail(UNKNOWN_ACCOUNT, "target", "member", 401),
        targets={"own-primary"},
        shapes={"inactive"},
    ),
    expect(
        "reset-request",
        Mail(RESET_CODE, "target", "request", 401),
        targets={"own-primary"},
        shapes=UNVERIFIED_SHAPES,
    ),
    expect(
        "reset-request",
        Mail(RESET_CODE, "target", "member", 401),
        targets={"own-primary", "own-verified-secondary"},
        states=ANONYMOUS,
    ),
    expect(
        "reset-request",
        Mail(RESET_CODE, "target", "member", 200),
        targets={"own-primary", "own-verified-secondary"},
    ),
    expect(
        "reset-request",
        Mail(RESET_CODE, "target", "other", 401),
        targets={"other-account"},
        states=ANONYMOUS,
    ),
    expect("reset-request", Mail(RESET_CODE, "target", "other", 200), targets={"other-account"}),
    expect(
        "reset-request",
        Mail(UNKNOWN_ACCOUNT, "target", "inactive", 401),
        targets={"inactive-account"},
        states=ANONYMOUS,
    ),
    expect(
        "reset-request",
        Mail(UNKNOWN_ACCOUNT, "target", "inactive", 200),
        targets={"inactive-account"},
    ),
    expect(
        "reset-request",
        Mail(UNKNOWN_ACCOUNT, "target", "request", 401),
        targets={"own-unverified-secondary", "unknown"},
        states=ANONYMOUS,
    ),
    expect(
        "reset-request",
        Mail(UNKNOWN_ACCOUNT, "target", "request", 200),
        targets={"own-unverified-secondary", "unknown", "pending-address"},
    ),
    expect(
        "reset",
        Mail(RESET_NOTICE, "primary", "member", 401),
        targets={"own-primary", "own-verified-secondary"},
        states=ANONYMOUS,
    ),
    expect(
        "reset",
        Mail(RESET_NOTICE, "primary", "member", 200),
        targets={"own-primary", "own-verified-secondary"},
    ),
    expect(
        "reset",
        Mail(RESET_NOTICE, "target", "other", 401),
        targets={"other-account"},
        states=ANONYMOUS,
    ),
    expect("reset", Mail(RESET_NOTICE, "target", "other", 200), targets={"other-account"}),
    expect("verify", NoMail(409, "no verification is pending"), states=NO_VERIFICATION),
    expect(
        "verify",
        NoMail(200, "a first verification has no previous address to notify"),
        states={"pending-signup"},
    ),
    expect("verify", Mail(EMAIL_CHANGED, "primary", "member", 200), states=PENDING_CHANGES),
    expect("resend", NoMail(409, "no verification is pending"), states=NO_VERIFICATION),
    expect(
        "resend",
        Mail(SIGNUP_CODE, "primary", "member", 200),
        states={"pending-signup"},
        shapes={"unverified-new"},
    ),
    expect(
        "resend",
        Mail(CHANGE_CODE, "primary", "member", 200),
        states={"pending-signup"},
        shapes={"unverified-returning"},
    ),
    expect("resend", Mail(SIGNUP_CODE, "fresh", "request", 200), states={"pending-signup"}),
    expect("resend", Mail(CHANGE_CODE, "pending", "member", 200), states=PENDING_CHANGES),
    expect("add-address", NoMail(409, "no session and no change stage"), states=ANONYMOUS),
    expect("add-address", NoMail(400, "already an address of the account"), targets=OWN),
    expect(
        "add-address",
        NoMail(400, "already an address of the account"),
        targets={"pending-address"},
        states={"pending-stored-change"},
    ),
    expect("add-address", NoMail(401, "needs reauthentication"), states=NOT_REAUTHENTICATED),
    expect(
        "add-address",
        Mail(CHANGE_CODE, "target", "member", 200),
        targets={*ELSEWHERE, "pending-address"},
    ),
    expect("select-address", NoMail(401, "no session"), states=ANONYMOUS),
    expect(
        "select-address",
        NoMail(400, "the address is already verified"),
        targets={"own-primary", "own-verified-secondary"},
    ),
    expect(
        "select-address",
        Mail(CHANGE_CODE, "target", "member", 200),
        targets={"own-unverified-secondary", "pending-address"},
    ),
    expect("select-address", NoMail(400, "not an address of the account"), targets=ELSEWHERE),
    expect("make-primary", NoMail(401, "no session"), states=ANONYMOUS),
    expect(
        "make-primary",
        NoMail(400, "an unverified address cannot become primary"),
        targets={"own-unverified-secondary", "pending-address"},
    ),
    expect("make-primary", NoMail(400, "not an address of the account"), targets=ELSEWHERE),
    expect("make-primary", NoMail(401, "needs reauthentication"), states=NOT_REAUTHENTICATED),
    expect(
        "make-primary", NoMail(200, "already primary: nothing changes"), targets={"own-primary"}
    ),
    expect(
        "make-primary",
        Mail(EMAIL_CHANGED, "primary", "member", 200),
        targets={"own-verified-secondary"},
    ),
    expect("remove-address", NoMail(401, "no session"), states=ANONYMOUS),
    expect("remove-address", NoMail(400, "the primary address stays"), targets={"own-primary"}),
    expect("remove-address", NoMail(400, "not an address of the account"), targets=ELSEWHERE),
    expect(
        "remove-address",
        NoMail(200, "aborts the pending change"),
        targets={"pending-address"},
        states={"pending-change"},
    ),
    expect("remove-address", NoMail(401, "needs reauthentication"), states=NOT_REAUTHENTICATED),
    expect(
        "remove-address",
        Mail(EMAIL_DELETED, "primary", "member", 200),
        targets={"own-verified-secondary", "own-unverified-secondary", "pending-address"},
    ),
    expect("password", NoMail(401, "no session"), states=ANONYMOUS),
    expect(
        "password",
        NoMail(401, "a first password needs reauthentication"),
        shapes=PASSWORDLESS,
        states=NOT_REAUTHENTICATED,
    ),
    expect("password", Mail(SET_NOTICE, "primary", "member", 401), shapes=PASSWORDLESS),
    expect("password", Mail(CHANGED_NOTICE, "primary", "member", 401)),
    expect("reauthentication-code", NoMail(401, "no session"), states=ANONYMOUS),
    expect(
        "reauthentication-code",
        Mail(LOGIN_CODE, "target", "member", 200),
        targets={"own-primary", "own-verified-secondary"},
    ),
    expect(
        "reauthentication-code",
        NoMail(400, "not a verified address of the account"),
        targets={"own-unverified-secondary", "pending-address", *ELSEWHERE},
    ),
    expect(
        "totp-on",
        NoMail(400, "two-factor authentication needs every address verified"),
        shapes={"unverified-secondary"},
        states=SIGNED,
    ),
    *notifies("totp-on", TOTP_ON),
    *notifies("totp-off", TOTP_OFF),
    *notifies("recovery-codes", RECOVERY_CODES),
    expect("disconnect", NoMail(401, "no session"), states=ANONYMOUS),
    expect(
        "disconnect",
        NoMail(400, "the last way to sign in stays"),
        shapes={"passwordless"},
    ),
    expect("disconnect", NoMail(401, "needs reauthentication"), states=NOT_REAUTHENTICATED),
    expect("disconnect", Mail(DISCONNECTED, "primary", "member", 200)),
    expect(
        "provider-signup",
        NoMail(
            200, "Google's verified address becomes the primary; the entered one stays unverified"
        ),
        targets={"unknown"},
        states={"pending-provider-signup"},
        providers={"google"},
    ),
    expect("provider-signup", Mail(SIGNUP_CODE, "target", "request", 401), targets={"unknown"}),
    expect(
        "provider-signup",
        Mail(SIGNUP_CODE, "target", "request", 401),
        targets={"pending-address"},
        states={"pending-change"},
    ),
    expect(
        "provider-signup",
        Mail(EXISTS, "target", "request", 401),
        targets={"own-primary"},
        shapes=UNVERIFIED_SHAPES,
    ),
    expect(
        "provider-signup",
        Mail(EXISTS, "target", "request", 401),
        targets={"own-unverified-secondary", "pending-address"},
    ),
    expect(
        "provider-signup",
        Mail(EXISTS, "target", "member", 401),
        targets={"own-primary", "own-verified-secondary"},
    ),
    expect("provider-signup", Mail(EXISTS, "target", "other", 401), targets={"other-account"}),
    expect(
        "provider-signup", Mail(EXISTS, "target", "inactive", 401), targets={"inactive-account"}
    ),
    expect(
        "provider-login",
        Mail(SIGNUP_CODE, "target", "member", 302),
        targets={"own-primary"},
        shapes={"unverified-new"},
    ),
    expect(
        "provider-login",
        Mail(CHANGE_CODE, "target", "member", 302),
        targets={"own-primary"},
        shapes={"unverified-returning"},
    ),
    expect(
        "provider-login",
        NoMail(302, "signs in, asks for the sign-up form or refuses an inactive account"),
    ),
    expect(
        "provider-connect",
        NoMail(302, "connecting needs a reauthenticated session"),
        states=GATED["provider-connect"],
    ),
    expect("provider-connect", Mail(CONNECTED, "primary", "member", 302)),
)


@dataclass(frozen=True)
class Case:
    combination: Combination
    language: str
    expected: Mail | NoMail | None

    def __str__(self) -> str:
        c = self.combination
        return f"{c.endpoint}-{c.shape}-{c.target}-{c.state}-{c.provider}-{self.language}"


def row_of(combination: Combination) -> int:
    return next((index for index, row in enumerate(EXPECTED) if row.matches(combination)), -1)


def languages_of(expected: Mail | NoMail | None, position: int) -> tuple[str, ...]:
    if isinstance(expected, NoMail):
        return ("en",)
    if expected is None or position == 0:
        return LANGUAGES
    return (LANGUAGES[position % len(LANGUAGES)],)


def matrix() -> list[Case]:
    cases: list[Case] = []
    positions: Counter[tuple[int, str]] = Counter()
    for endpoint, shape, target, state, provider in product(
        ENDPOINTS, SHAPES, TARGETS, STATES, PROVIDER_CHOICES
    ):
        combination = Combination(endpoint, shape, target, state, provider)
        if any(rule(combination) for _, rule in NOT_APPLICABLE):
            continue
        row = row_of(combination)
        expected = EXPECTED[row].outcome if row >= 0 else None
        languages = languages_of(expected, positions[row, provider])
        positions[row, provider] += 1
        cases.extend(Case(combination, language, expected) for language in languages)
    return cases


CASES = matrix()


def rotates(case: Case) -> bool:
    combination, expected = case.combination, case.expected
    if not isinstance(expected, Mail):
        return False
    return (
        (combination.endpoint == "resend" and combination.state in PENDING_VERIFICATION)
        or (combination.endpoint == "select-address" and combination.state in PENDING_CHANGES)
        or (
            combination.endpoint == "add-address"
            and combination.state in PENDING_CHANGES
            and combination.target in {"unknown", "pending-address"}
        )
        or (
            combination.endpoint == "reauthentication-code"
            and combination.state == "reauthentication-pending"
        )
    )


@dataclass
class World:
    member: User
    recovery_codes: list[str]


@dataclass
class Session:
    client: Client
    code: str


@dataclass
class Act:
    case: Case
    session: Session
    world: World
    profile: dict[str, Any]
    monkeypatch: pytest.MonkeyPatch

    @property
    def target(self) -> str:
        return address_of(self.case, self.case.combination.target)

    @property
    def provider(self) -> str:
        return self.case.combination.provider


def pending_address(case: Case) -> str:
    return UNVERIFIED if case.combination.state == "pending-stored-change" else CHANGE


def address_of(case: Case, target: str) -> str:
    return pending_address(case) if target == "pending-address" else ADDRESSES.get(target, "")


def provider_requests(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    profile: dict[str, Any] = {}

    def request(
        self: requests.Session, method: str, url: str, *args: Any, **kwargs: Any
    ) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response.headers["content-type"] = "application/json"
        keys = (
            {"id", "mail", "userPrincipalName"}
            if "graph.microsoft.com" in url
            else {"sub", "email", "email_verified"}
        )
        response._content = json.dumps(
            {"access_token": "test-access-token", "token_type": "Bearer", "expires_in": 3600}
            if url.endswith("/token")
            else {key: value for key, value in profile.items() if key in keys}
        ).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return profile


def matrix_settings() -> dict[str, Any]:
    # A non-UTC server zone proves that notices convert their time to UTC.
    return {
        "MAILERS": {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
        "TIME_ZONE": "Europe/Istanbul",
        "SOCIALACCOUNT_PROVIDERS": PROVIDERS,
        "HEADLESS_FRONTEND_URLS": settings.HEADLESS_FRONTEND_URLS
        | {"socialaccount_login_error": CALLBACK},
    }


def assume_identity(
    profile: dict[str, Any], uid: str, email: str, *, verified: bool = False
) -> None:
    profile.update(
        sub=uid, id=uid, email=email, mail=email, userPrincipalName=email, email_verified=verified
    )


def one_minute_later(monkeypatch: pytest.MonkeyPatch) -> None:
    # confirm_email allows one mail per address and minute; only the limiter's clock moves.
    later = time.time() + 61
    monkeypatch.setattr(
        ratelimit,
        "time",
        SimpleNamespace(time=lambda: later, monotonic=time.monotonic, sleep=time.sleep),
    )


def browser(language: str | None = None, client_class: type[Client] = Client) -> Client:
    headers = {"User-Agent": HOSTILE_AGENT}
    if language is not None:
        headers["Accept-Language"] = language
    return client_class(enforce_csrf_checks=True, headers=headers)


def call(client: Client, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
    token = client.get("/api/session/").json()["csrf_token"]
    return getattr(client, method)(
        AUTH + path, data or {}, content_type="application/json", HTTP_X_CSRFTOKEN=token
    )


def only_mail() -> EmailMultiAlternatives:
    (message,) = mail.outbox
    assert isinstance(message, EmailMultiAlternatives)
    return message


def code_in(message: EmailMultiAlternatives) -> str:
    match = CODE.search(str(message.body))
    assert match is not None
    return match.group(1)


def mailed_code() -> str:
    code = code_in(only_mail())
    mail.outbox.clear()
    return code


def account(
    email: str, username: str, language: str, *, verified: bool = True, active: bool = True
) -> User:
    user = User.objects.create_user(
        username=username, email=email, password=PASSWORD, is_active=active
    )
    EmailAddress.objects.create(user=user, email=email, primary=True, verified=verified)
    UserLanguagePreference.objects.create(user=user, language=language)
    return user


def build_world(case: Case) -> World:
    combination, language = case.combination, case.language
    shape = combination.shape
    member = account(
        PRIMARY,
        "member",
        language,
        verified=shape not in UNVERIFIED_SHAPES,
        active=shape != "inactive",
    )
    if shape in PASSWORDLESS:
        member.set_unusable_password()
        member.save(update_fields=["password"])
    if shape == "passwordless-linked":
        provider = OTHER_PROVIDER[combination.provider]
        SocialAccount.objects.create(user=member, provider=provider, uid="other-identity")
    if shape == "verified-secondary":
        EmailAddress.objects.create(user=member, email=SECONDARY, verified=True)
    if shape == "unverified-secondary":
        EmailAddress.objects.create(user=member, email=UNVERIFIED)
    if shape == "unverified-returning":
        User.objects.filter(pk=member.pk).update(last_login=datetime.now(UTC))
    owners = {
        "own-primary": member,
        "other-account": account(OTHER_ADDRESS, "other", OTHER[language]),
        "inactive-account": account(INACTIVE_ADDRESS, "inactive", language, active=False),
    }
    recovery_codes: list[str] = []
    if ENDPOINTS[combination.endpoint].mfa:
        TOTP.activate(member, generate_totp_secret())
        recovery_codes = RecoveryCodes.activate(member).generate_codes()
    if combination.endpoint == "disconnect":
        SocialAccount.objects.create(
            user=member, provider=combination.provider, uid="linked-identity"
        )
    if combination.endpoint == "provider-login" and combination.target in owners:
        SocialAccount.objects.create(
            user=owners[combination.target], provider=combination.provider, uid="linked-identity"
        )
    return World(member, recovery_codes)


def sign_in(client: Client, case: Case, world: World) -> None:
    if case.combination.shape in PASSWORDLESS:
        client.force_login(world.member)
        return
    credentials = {"email": PRIMARY, "password": PASSWORD}
    if world.recovery_codes:
        assert submit(client, "auth/login", credentials).status_code == 401
        code = {"code": world.recovery_codes[0]}
        assert submit(client, "auth/2fa/authenticate", code).status_code == 200
        return
    assert submit(client, "auth/login", credentials).status_code == 200


def request_reauthentication_code(client: Client) -> str:
    request = {"email": PRIMARY}
    assert submit(client, "auth/reauthenticate/code/request", request).status_code == 200
    return mailed_code()


def reauthenticate(client: Client, case: Case, world: World) -> None:
    if case.combination.shape in PASSWORDLESS:
        confirmation = {"code": request_reauthentication_code(client)}
        assert submit(client, "auth/reauthenticate/code/confirm", confirmation).status_code == 200
    elif world.recovery_codes:
        code = {"code": world.recovery_codes[1]}
        assert submit(client, "auth/2fa/reauthenticate", code).status_code == 200
    else:
        assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200


def enter(case: Case, world: World, profile: dict[str, Any], client_class: type[Client]) -> Session:
    combination = case.combination
    state = combination.state
    if state in ANONYMOUS:
        client = browser(OTHER[case.language], client_class)
        if state == "pending-provider-signup":
            assume_identity(profile, "pending-identity", "pending@gmail.com", verified=True)
            provider = combination.provider if combination.provider != "none" else "google"
            through_provider(client, provider, "login")
            assert "socialaccount_sociallogin" in client.session
            return Session(client, "")
        if state == "signed-out":
            return Session(client, "")
        if combination.shape in UNVERIFIED_SHAPES:
            credentials = {"email": PRIMARY, "password": PASSWORD}
            assert submit(client, "auth/login", credentials).status_code == 401
        else:
            details = {"email": FRESH, "company_name": "Fresh Signup"}
            assert signup(client, details).status_code == 401
        return Session(client, mailed_code())
    client = browser(client_class=client_class)
    sign_in(client, case, world)
    if state == "signed-in":
        return Session(client, "")
    if state == "reauthentication-pending":
        return Session(client, request_reauthentication_code(client))
    reauthenticate(client, case, world)
    if state == "reauthenticated":
        return Session(client, "")
    if state == "pending-stored-change":
        assert call(client, "put", "account/email", {"email": UNVERIFIED}).status_code == 200
    else:
        assert submit(client, "account/email", {"email": CHANGE}).status_code == 200
    return Session(client, mailed_code())


def provider_redirect(client: Client, provider: str, process: str) -> tuple[Any, str | None]:
    token = client.get("/api/session/").json()["csrf_token"]
    response = client.post(
        AUTH + "auth/provider/redirect",
        {"provider": provider, "process": process, "callback_url": CALLBACK},
        HTTP_X_CSRFTOKEN=token,
    )
    if response.status_code != 200:
        return response, None
    return response, parse_qs(urlsplit(provider_destination(response)).query)["state"][0]


def through_provider(client: Client, provider: str, process: str) -> Any:
    response, state = provider_redirect(client, provider, process)
    return response if state is None else complete(client, state, provider)


def act_reset(act: Act) -> Any:
    submit(act.session.client, "auth/password/request", {"email": act.target})
    reset = {"key": mailed_code(), "password": PASSWORD + "reset"}
    return submit(act.session.client, "auth/password/reset", reset)


def act_password(act: Act) -> Any:
    change = {"new_password": PASSWORD + "new"}
    if act.case.combination.shape not in PASSWORDLESS:
        change["current_password"] = PASSWORD
    return submit(act.session.client, "account/password/change", change)


def act_totp_on(act: Act) -> Any:
    client = act.session.client
    setup = client.get(AUTH + "account/authenticators/totp").json()
    secret = setup.get("meta", {}).get("secret", "")
    now = time.time()
    # The CSRF and activation requests can cross a 30-second TOTP boundary.
    act.monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now))
    code = totp_code(secret, now) if secret else "000000"
    return submit(client, "account/authenticators/totp", {"code": code})


def act_provider_signup(act: Act) -> Any:
    if act.case.combination.state != "pending-provider-signup":
        assume_identity(act.profile, "signup-identity", act.target)
        response, state = provider_redirect(act.session.client, act.provider, "login")
        if state is None:
            return response
        complete(act.session.client, state, act.provider)
    details = {"email": act.target, "company_name": "Provider Signup"}
    return submit(act.session.client, "auth/provider/signup", details)


def act_provider_login(act: Act) -> Any:
    unknown = act.case.combination.target == "unknown"
    assume_identity(act.profile, "new-identity" if unknown else "linked-identity", act.target)
    return through_provider(act.session.client, act.provider, "login")


def act_provider_connect(act: Act) -> Any:
    assume_identity(act.profile, "connect-identity", "connect@example.com", verified=True)
    return through_provider(act.session.client, act.provider, "connect")


ACTIONS: dict[str, Callable[[Act], Any]] = {
    "signup": lambda a: signup(
        a.session.client, {"email": a.target, "company_name": "Matrix Signup"}
    ),
    "login": lambda a: submit(
        a.session.client, "auth/login", {"email": a.target, "password": PASSWORD}
    ),
    "reset-request": lambda a: submit(
        a.session.client, "auth/password/request", {"email": a.target}
    ),
    "reset": act_reset,
    "verify": lambda a: submit(a.session.client, "auth/email/verify", {"key": a.session.code}),
    "resend": lambda a: submit(a.session.client, "auth/email/verify/resend", {}),
    "add-address": lambda a: submit(a.session.client, "account/email", {"email": a.target}),
    "select-address": lambda a: call(a.session.client, "put", "account/email", {"email": a.target}),
    "make-primary": lambda a: call(
        a.session.client, "patch", "account/email", {"email": a.target, "primary": True}
    ),
    "remove-address": lambda a: call(
        a.session.client, "delete", "account/email", {"email": a.target}
    ),
    "password": act_password,
    "reauthentication-code": lambda a: submit(
        a.session.client, "auth/reauthenticate/code/request", {"email": a.target}
    ),
    "totp-on": act_totp_on,
    "totp-off": lambda a: call(a.session.client, "delete", "account/authenticators/totp"),
    "recovery-codes": lambda a: submit(
        a.session.client, "account/authenticators/recovery-codes", {}
    ),
    "disconnect": lambda a: call(
        a.session.client,
        "delete",
        "account/providers",
        {"provider": a.provider, "account": "linked-identity"},
    ),
    "provider-signup": act_provider_signup,
    "provider-login": act_provider_login,
    "provider-connect": act_provider_connect,
}


def html(message: EmailMultiAlternatives) -> str:
    ((content, mimetype),) = message.alternatives
    assert mimetype == "text/html"
    assert isinstance(content, str)
    return content


def assert_account_mail(
    message: EmailMultiAlternatives, *, prefix: str, to: str, language: str, subject: str
) -> None:
    assert message.to == [to]
    assert message.extra_headers["X-Pyracast-Mail"] == prefix
    assert message.subject == subject
    assert f'<html lang="{language}">' in html(message)


def assert_code_mail(
    message: EmailMultiAlternatives, *, prefix: str, to: str, language: str, minutes: int
) -> None:
    assert_account_mail(
        message, prefix=prefix, to=to, language=language, subject=CODE_SUBJECTS[language][prefix]
    )
    code = code_in(message)
    instruction, expiry = CODE_COPY[language]
    for content in (str(message.body), html(message)):
        assert code in content
        assert instruction in content
        assert expiry.format(minutes=minutes) in content


def assert_notice(
    message: EmailMultiAlternatives, *, prefix: str, to: str, language: str, **values: str
) -> None:
    subject, sentence = NOTICE_COPY[language][prefix]
    assert_account_mail(message, prefix=prefix, to=to, language=language, subject=subject)
    assert sentence.format(**values) in str(message.body)
    assert escape(sentence.format(**values)) in html(message)


def assert_security_details(
    message: EmailMultiAlternatives, *, language: str, since: datetime
) -> None:
    copy = SECURITY_COPY[language]
    minute = since.replace(second=0, microsecond=0)
    times = []
    with translation.override(language):
        while minute <= datetime.now(UTC):
            times.append(f"{copy['time']}: {dateformat.format(minute, 'j F Y H:i')} UTC")
            minute += timedelta(minutes=1)
    for content in (str(message.body), html(message)):
        assert any(time in content for time in times)
        assert copy["device"] in content
        assert FIREFOX not in content
        assert "<script" not in content
        assert "X-Injected" not in content
        assert copy["ip"] in content
        assert copy["warning"] in content
        assert "https://testserver/app/" in content
    assert copy["link"] in html(message)


def assert_action(message: EmailMultiAlternatives, *, prefix: str, language: str, url: str) -> None:
    label = ACTION_LABELS[language][prefix]
    assert f"{label}: {url}" in str(message.body)
    assert escape(label) in html(message)
    assert escape(url) in html(message)
    assert SECURITY_COPY[language]["warning"] not in str(message.body)


def recipient(role: str, case: Case) -> str:
    return {
        "target": address_of(case, case.combination.target),
        "primary": PRIMARY,
        "fresh": FRESH,
        "pending": pending_address(case),
    }[role]


def language_of(role: str, case: Case) -> str:
    request = OTHER[case.language] if case.combination.state in ANONYMOUS else case.language
    return {
        "member": case.language,
        "other": OTHER[case.language],
        "inactive": case.language,
        "request": request,
    }[role]


def sentence_values(template: str, case: Case, to: str) -> dict[str, str]:
    combination = case.combination
    if template in ACTION_URLS:
        return {"email": to}
    if template == EMAIL_CHANGED:
        if combination.endpoint == "verify":
            return {"from_email": PRIMARY, "to_email": pending_address(case)}
        return {"from_email": PRIMARY, "to_email": address_of(case, combination.target)}
    if template == EMAIL_DELETED:
        return {"deleted_email": address_of(case, combination.target)}
    if template in {CONNECTED, DISCONNECTED}:
        return {"provider": PROVIDER_NAMES[combination.provider]}
    return {}


def assert_contract(message: EmailMultiAlternatives, case: Case, since: datetime) -> None:
    expected = case.expected
    assert isinstance(expected, Mail)
    to, language = recipient(expected.to, case), language_of(expected.language, case)
    if expected.template in CODE_MINUTES:
        assert_code_mail(
            message,
            prefix=expected.template,
            to=to,
            language=language,
            minutes=CODE_MINUTES[expected.template],
        )
        return
    values = sentence_values(expected.template, case, to)
    assert_notice(message, prefix=expected.template, to=to, language=language, **values)
    if expected.template in ACTION_URLS:
        url = ACTION_URLS[expected.template]
        assert_action(message, prefix=expected.template, language=language, url=url)
    else:
        assert_security_details(message, language=language, since=since)


def assert_rotation(client: Client, expected: Mail, old: str, new: str) -> None:
    path, field = CONFIRMATIONS[expected.template]
    assert new != old
    assert submit(client, path, {field: old}).status_code == 400
    assert submit(client, path, {field: new}).status_code == 200


def run_case(
    case: Case, profile: dict[str, Any], monkeypatch: pytest.MonkeyPatch, client_class: type[Client]
) -> None:
    assert case.expected is not None, f"no expectation declared for {case}"
    world = build_world(case)
    session = enter(case, world, profile, client_class)
    mail.outbox.clear()
    one_minute_later(monkeypatch)
    since = datetime.now(UTC)

    response = ACTIONS[case.combination.endpoint](Act(case, session, world, profile, monkeypatch))

    sent = [(message.extra_headers.get("X-Pyracast-Mail"), message.to) for message in mail.outbox]
    if isinstance(case.expected, NoMail):
        assert (response.status_code, sent) == (case.expected.status, []), case.expected.reason
        return
    to = recipient(case.expected.to, case)
    assert (response.status_code, sent) == (case.expected.status, [(case.expected.template, [to])])
    message = only_mail()
    assert_contract(message, case, since)
    if rotates(case):
        assert_rotation(session.client, case.expected, session.code, code_in(message))
