import json
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

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
from django.test import Client, override_settings
from django.utils import dateformat, translation
from django.utils.html import escape

from pyracast.accounts.account_mail import device_label
from pyracast.accounts.adapters import AccountAdapter
from pyracast.accounts.models import UserLanguagePreference
from tests.test_auth_lifecycle import PASSWORD, member, submit, totp_code
from tests.test_company_registration import signup
from tests.test_social_accounts import CALLBACK, PROVIDERS, begin, complete

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
AUTH = "/api/auth/browser/v1/"
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


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    # A non-UTC server zone proves that notices convert their time to UTC.
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
        TIME_ZONE="Europe/Istanbul",
    ):
        yield


@pytest.fixture(params=["en", "tr"])
def language(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def social_providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    profile: dict[str, Any] = {
        "sub": "google-identity",
        "email": "solar@gmail.com",
        "email_verified": True,
    }

    def request(
        self: requests.Session, method: str, url: str, *args: Any, **kwargs: Any
    ) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response.headers["content-type"] = "application/json"
        response._content = json.dumps(
            {"access_token": "test-access-token", "token_type": "Bearer", "expires_in": 3600}
            if url.endswith("/token")
            else profile
        ).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    with override_settings(
        SOCIALACCOUNT_PROVIDERS=PROVIDERS,
        HEADLESS_FRONTEND_URLS=settings.HEADLESS_FRONTEND_URLS
        | {"socialaccount_login_error": CALLBACK},
    ):
        yield profile


def one_minute_later(monkeypatch: pytest.MonkeyPatch) -> None:
    # confirm_email allows one mail per address and minute; only the limiter's clock moves.
    later = time.time() + 61
    monkeypatch.setattr(
        ratelimit,
        "time",
        SimpleNamespace(time=lambda: later, monotonic=time.monotonic, sleep=time.sleep),
    )


def browser(language: str | None = None) -> Client:
    headers = {"User-Agent": HOSTILE_AGENT}
    if language is not None:
        headers["Accept-Language"] = language
    return Client(enforce_csrf_checks=True, headers=headers)


def account(language: str) -> User:
    user = member()
    UserLanguagePreference.objects.create(user=user, language=language)
    return user


def signed_in(user: User, *, reauthenticated: bool = False) -> Client:
    client = browser()
    assert (
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200
    )
    if reauthenticated:
        assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    mail.outbox.clear()
    return client


def two_factor_account(language: str) -> tuple[Client, User]:
    user = account(language)
    TOTP.activate(user, generate_totp_secret())
    first, second, *_ = RecoveryCodes.activate(user).generate_codes()
    client = browser()
    assert (
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 401
    )
    assert submit(client, "auth/2fa/authenticate", {"code": first}).status_code == 200
    assert submit(client, "auth/2fa/reauthenticate", {"code": second}).status_code == 200
    mail.outbox.clear()
    return client, user


def call(client: Client, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
    token = client.get("/api/session/").json()["csrf_token"]
    return getattr(client, method)(
        AUTH + path, data or {}, content_type="application/json", HTTP_X_CSRFTOKEN=token
    )


def only_mail() -> EmailMultiAlternatives:
    (message,) = mail.outbox
    assert isinstance(message, EmailMultiAlternatives)
    return message


def mailed_code() -> str:
    match = CODE.search(str(only_mail().body))
    assert match is not None
    mail.outbox.clear()
    return match.group(1)


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
    code = CODE.search(str(message.body))
    assert code is not None
    instruction, expiry = CODE_COPY[language]
    for content in (str(message.body), html(message)):
        assert code.group(1) in content
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


def test_signup_mails_the_verification_code_in_the_request_language(language: str) -> None:
    assert signup(browser(language)).status_code == 401

    assert_code_mail(
        only_mail(),
        prefix="account/email/email_confirmation_signup",
        to="owner@example.com",
        language=language,
        minutes=10,
    )


def test_unverified_sign_in_mails_a_new_code_in_the_stored_language(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert signup(browser(language)).status_code == 401
    mail.outbox.clear()
    one_minute_later(monkeypatch)
    credentials = {"email": "owner@example.com", "password": PASSWORD}

    response = submit(browser(OTHER[language]), "auth/login", credentials)

    assert response.status_code == 401
    assert {"id": "verify_email", "is_pending": True} in response.json()["data"]["flows"]
    assert_code_mail(
        only_mail(),
        prefix="account/email/email_confirmation_signup",
        to="owner@example.com",
        language=language,
        minutes=10,
    )


def test_verification_resend_mails_a_new_code_in_the_stored_language(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = browser(language)
    assert signup(client).status_code == 401
    first = mailed_code()
    one_minute_later(monkeypatch)
    client.defaults["HTTP_ACCEPT_LANGUAGE"] = OTHER[language]

    response = submit(client, "auth/email/verify/resend", {})

    assert response.status_code == 200
    message = only_mail()
    assert_code_mail(
        message,
        prefix="account/email/email_confirmation_signup",
        to="owner@example.com",
        language=language,
        minutes=10,
    )
    assert first not in str(message.body)


def test_signup_with_a_taken_address_notifies_the_owner_in_their_language(language: str) -> None:
    user = account(language)

    assert signup(browser(OTHER[language]), {"email": user.email}).status_code == 401

    message = only_mail()
    prefix = "account/email/account_already_exists"
    assert_notice(message, prefix=prefix, to=user.email, language=language, email=user.email)
    assert_action(
        message,
        prefix=prefix,
        language=language,
        url=settings.HEADLESS_FRONTEND_URLS["account_reset_password"],
    )


def test_reset_request_for_an_unknown_address_notifies_it_in_the_request_language(
    language: str,
) -> None:
    address = "missing@example.com"

    response = submit(browser(language), "auth/password/request", {"email": address})

    assert response.status_code == 401
    message = only_mail()
    prefix = "account/email/unknown_account"
    assert_notice(message, prefix=prefix, to=address, language=language, email=address)
    assert_action(
        message,
        prefix=prefix,
        language=language,
        url=settings.HEADLESS_FRONTEND_URLS["account_signup"],
    )


def test_reset_code_follows_the_saved_language_of_the_address(language: str) -> None:
    user = account(language)

    response = submit(browser(OTHER[language]), "auth/password/request", {"email": user.email})

    assert response.status_code == 401
    assert_code_mail(
        only_mail(),
        prefix="account/email/password_reset_code",
        to=user.email,
        language=language,
        minutes=10,
    )


def test_password_reset_completion_sends_a_security_notice(language: str) -> None:
    user = account(language)
    client = browser(OTHER[language])
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    code = mailed_code()
    since = datetime.now(UTC)

    response = submit(client, "auth/password/reset", {"key": code, "password": PASSWORD + "new"})

    assert response.status_code == 401
    message = only_mail()
    assert_notice(message, prefix="account/email/password_reset", to=user.email, language=language)
    assert_security_details(message, language=language, since=since)


def test_password_change_sends_a_security_notice(language: str) -> None:
    user = account(language)
    client = signed_in(user)
    since = datetime.now(UTC)

    response = submit(
        client,
        "account/password/change",
        {"current_password": PASSWORD, "new_password": PASSWORD + "new"},
    )

    assert response.status_code == 401
    message = only_mail()
    assert_notice(
        message, prefix="account/email/password_changed", to=user.email, language=language
    )
    assert_security_details(message, language=language, since=since)


def test_password_set_sends_a_security_notice(language: str) -> None:
    user = account(language)
    user.set_unusable_password()
    user.save(update_fields=["password"])
    client = browser()
    client.force_login(user)
    request = {"email": user.email}
    assert submit(client, "auth/reauthenticate/code/request", request).status_code == 200
    confirmation = {"code": mailed_code()}
    assert submit(client, "auth/reauthenticate/code/confirm", confirmation).status_code == 200
    since = datetime.now(UTC)

    response = submit(client, "account/password/change", {"new_password": PASSWORD + "set"})

    assert response.status_code == 401
    message = only_mail()
    assert_notice(message, prefix="account/email/password_set", to=user.email, language=language)
    assert_security_details(message, language=language, since=since)


def test_reauthentication_code_is_mailed_in_the_stored_language(language: str) -> None:
    user = account(language)
    client = signed_in(user)

    response = submit(client, "auth/reauthenticate/code/request", {"email": user.email})

    assert response.status_code == 200
    assert_code_mail(
        only_mail(),
        prefix="account/email/login_code",
        to=user.email,
        language=language,
        minutes=3,
    )


def test_reauthentication_code_resend_is_disabled_and_mails_nothing() -> None:
    user = member()
    client = signed_in(user)
    assert (
        submit(client, "auth/reauthenticate/code/request", {"email": user.email}).status_code == 200
    )
    mail.outbox.clear()

    response = submit(client, "auth/reauthenticate/code/resend", {})

    assert response.status_code == 409
    assert mail.outbox == []


def test_email_change_mails_the_code_to_the_new_address(language: str) -> None:
    user = account(language)
    client = signed_in(user, reauthenticated=True)

    response = submit(client, "account/email", {"email": "new@example.com"})

    assert response.status_code == 200
    assert_code_mail(
        only_mail(),
        prefix="account/email/email_confirmation",
        to="new@example.com",
        language=language,
        minutes=10,
    )


def test_put_resends_the_pending_change_code_to_the_new_address(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = account(language)
    client = signed_in(user, reauthenticated=True)
    assert submit(client, "account/email", {"email": "new@example.com"}).status_code == 200
    first = mailed_code()
    one_minute_later(monkeypatch)

    response = call(client, "put", "account/email", {"email": "new@example.com"})

    assert response.status_code == 200
    message = only_mail()
    assert_code_mail(
        message,
        prefix="account/email/email_confirmation",
        to="new@example.com",
        language=language,
        minutes=10,
    )
    assert first not in str(message.body)


def test_put_mails_a_code_to_a_stored_unverified_address(language: str) -> None:
    user = account(language)
    EmailAddress.objects.create(user=user, email="pending@example.com")
    client = signed_in(user)

    response = call(client, "put", "account/email", {"email": "pending@example.com"})

    assert response.status_code == 200
    assert_code_mail(
        only_mail(),
        prefix="account/email/email_confirmation",
        to="pending@example.com",
        language=language,
        minutes=10,
    )


def test_primary_address_change_notifies_the_previous_primary_address(language: str) -> None:
    user = account(language)
    old = user.email
    EmailAddress.objects.create(user=user, email="second@example.com", verified=True)
    client = signed_in(user, reauthenticated=True)
    since = datetime.now(UTC)

    response = call(
        client, "patch", "account/email", {"email": "second@example.com", "primary": True}
    )

    assert response.status_code == 200
    message = only_mail()
    assert_notice(
        message,
        prefix="account/email/email_changed",
        to=old,
        language=language,
        from_email=old,
        to_email="second@example.com",
    )
    assert_security_details(message, language=language, since=since)


def test_email_change_notifies_the_old_address(language: str) -> None:
    user = account(language)
    old = user.email
    client = signed_in(user, reauthenticated=True)
    assert submit(client, "account/email", {"email": "new@example.com"}).status_code == 200
    code = mailed_code()
    since = datetime.now(UTC)

    response = submit(client, "auth/email/verify", {"key": code})

    assert response.status_code == 200
    message = only_mail()
    assert_notice(
        message,
        prefix="account/email/email_changed",
        to=old,
        language=language,
        from_email=old,
        to_email="new@example.com",
    )
    assert_security_details(message, language=language, since=since)


def test_removing_an_address_notifies_the_primary_address(language: str) -> None:
    user = account(language)
    EmailAddress.objects.create(user=user, email="second@example.com", verified=True)
    client = signed_in(user, reauthenticated=True)
    since = datetime.now(UTC)

    response = call(client, "delete", "account/email", {"email": "second@example.com"})

    assert response.status_code == 200
    message = only_mail()
    assert_notice(
        message,
        prefix="account/email/email_deleted",
        to=user.email,
        language=language,
        deleted_email="second@example.com",
    )
    assert_security_details(message, language=language, since=since)


def test_totp_activation_sends_a_security_notice(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = account(language)
    client = signed_in(user, reauthenticated=True)
    setup = client.get(AUTH + "account/authenticators/totp")
    assert setup.status_code == 404
    now = time.time()
    # The CSRF and activation requests can cross a 30-second TOTP boundary.
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now))
    code = totp_code(setup.json()["meta"]["secret"], now)
    since = datetime.now(UTC)

    response = submit(client, "account/authenticators/totp", {"code": code})

    assert response.status_code == 200
    message = only_mail()
    assert_notice(message, prefix="mfa/email/totp_activated", to=user.email, language=language)
    assert_security_details(message, language=language, since=since)


def test_totp_deactivation_sends_a_security_notice(language: str) -> None:
    client, user = two_factor_account(language)
    since = datetime.now(UTC)

    response = call(client, "delete", "account/authenticators/totp")

    assert response.status_code == 200
    message = only_mail()
    assert_notice(message, prefix="mfa/email/totp_deactivated", to=user.email, language=language)
    assert_security_details(message, language=language, since=since)


def test_recovery_code_regeneration_sends_a_security_notice(language: str) -> None:
    client, user = two_factor_account(language)
    since = datetime.now(UTC)

    response = submit(client, "account/authenticators/recovery-codes", {})

    assert response.status_code == 200
    message = only_mail()
    assert_notice(
        message, prefix="mfa/email/recovery_codes_generated", to=user.email, language=language
    )
    assert_security_details(message, language=language, since=since)


@pytest.mark.usefixtures("social_providers")
def test_provider_connect_sends_a_security_notice(language: str) -> None:
    user = account(language)
    client = signed_in(user, reauthenticated=True)
    since = datetime.now(UTC)

    complete(client, begin(client, process="connect"))

    message = only_mail()
    assert_notice(
        message,
        prefix="socialaccount/email/account_connected",
        to=user.email,
        language=language,
        provider="Google",
    )
    assert_security_details(message, language=language, since=since)


@pytest.mark.usefixtures("social_providers")
def test_provider_disconnect_sends_a_security_notice(language: str) -> None:
    user = account(language)
    linked = SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    client = signed_in(user, reauthenticated=True)
    since = datetime.now(UTC)

    response = call(
        client, "delete", "account/providers", {"provider": linked.provider, "account": linked.uid}
    )

    assert response.status_code == 200
    message = only_mail()
    assert_notice(
        message,
        prefix="socialaccount/email/account_disconnected",
        to=user.email,
        language=language,
        provider="Google",
    )
    assert_security_details(message, language=language, since=since)


def test_provider_signup_with_an_unverified_address_mails_the_code(
    language: str, social_providers: dict[str, Any]
) -> None:
    social_providers["email_verified"] = False
    client = browser(language)
    complete(client, begin(client))

    response = submit(
        client, "auth/provider/signup", {"email": "solar@gmail.com", "company_name": "Social Solar"}
    )

    assert response.status_code == 401
    assert_code_mail(
        only_mail(),
        prefix="account/email/email_confirmation_signup",
        to="solar@gmail.com",
        language=language,
        minutes=10,
    )


def test_provider_signup_with_a_registered_address_notifies_the_owner_in_their_language(
    language: str, social_providers: dict[str, Any]
) -> None:
    user = account(language)
    social_providers["email"] = user.email
    client = browser(OTHER[language])
    complete(client, begin(client))

    response = submit(
        client, "auth/provider/signup", {"email": user.email, "company_name": "Other"}
    )

    assert response.status_code == 401
    message = only_mail()
    prefix = "account/email/account_already_exists"
    assert_notice(message, prefix=prefix, to=user.email, language=language, email=user.email)
    assert_action(
        message,
        prefix=prefix,
        language=language,
        url=settings.HEADLESS_FRONTEND_URLS["account_reset_password"],
    )


def test_email_verified_notice_without_a_trigger_renders_the_product_copy() -> None:
    # allauth 65 ships this template but no flow sends it, so no view can drive it.
    user = member()
    message = AccountAdapter().render_mail(
        "account/email/email_confirm",
        user.email,
        {
            "user": user,
            "timestamp": datetime(2026, 11, 15, 15, 0, tzinfo=timezone(timedelta(hours=3))),
            "ip": "192.0.2.4",
            "user_agent": '<script>alert("unsafe")</script>',
        },
    )

    assert isinstance(message, EmailMultiAlternatives)
    assert message.subject == "Your email address was verified"
    assert message.extra_headers["X-Pyracast-Mail"] == "account/email/email_confirm"
    assert "The email address of your Pyracast account was verified." in str(message.body)
    assert "<script>" not in html(message)
    for content in (str(message.body), html(message)):
        assert "15 November 2026 12:00 UTC" in content
        assert "192.0.2.4" in content


@pytest.mark.parametrize(
    ("user_agent", "label"),
    [
        (FIREFOX, "Firefox on Windows"),
        (HOSTILE_AGENT, "Firefox on Windows"),
        (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/137.0 Safari/537.36",
            "Chrome on Linux",
        ),
        ("curl/8.0 (Android)", "Android"),
        ("Mozilla/5.0 Gecko/20100101 Firefox/139.0", "Firefox"),
        ("", "an unknown device"),
    ],
)
def test_device_label_names_browser_and_system_without_the_raw_agent(
    user_agent: str, label: str
) -> None:
    assert device_label(user_agent) == label


def test_notice_to_several_addresses_keeps_every_recipient() -> None:
    message = AccountAdapter().render_mail(
        "account/email/account_already_exists",
        ["first@example.com", "second@example.com"],
        {"email": "first@example.com", "password_reset_url": "https://testserver/app/"},
    )

    assert message.to == ["first@example.com", "second@example.com"]
    assert message.subject == "Your Pyracast account already exists"
    assert "https://testserver/app/" in str(message.body)
