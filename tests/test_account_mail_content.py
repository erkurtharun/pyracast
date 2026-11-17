import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import Client, override_settings
from django.utils import timezone

from pyracast.accounts.adapters import AccountAdapter
from pyracast.accounts.models import UserLanguagePreference
from tests.test_admin_recovery_security import RECOVERY, authenticated_account
from tests.test_admin_recovery_security import submit as submit_to
from tests.test_auth_lifecycle import PASSWORD, member, submit
from tests.test_company_registration import signup
from tests.test_social_accounts import CALLBACK, PROVIDERS, begin, complete

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
AUTH = "/api/auth/browser/v1/"
FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0"
CODE = re.compile(r"^\s*([A-Z0-9]{4}-[A-Z0-9]{4}|[0-9]{6})\s*$", re.MULTILINE)


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


@pytest.fixture
def social_providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def request(
        self: requests.Session, method: str, url: str, *args: Any, **kwargs: Any
    ) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response.headers["content-type"] = "application/json"
        response._content = json.dumps(
            {"access_token": "test-access-token", "token_type": "Bearer", "expires_in": 3600}
            if url.endswith("/token")
            else {"sub": "google-identity", "email": "solar@gmail.com", "email_verified": True}
        ).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    with override_settings(
        SOCIALACCOUNT_PROVIDERS=PROVIDERS,
        HEADLESS_FRONTEND_URLS=settings.HEADLESS_FRONTEND_URLS
        | {"socialaccount_login_error": CALLBACK},
    ):
        yield


def browser(language: str = "en") -> Client:
    return Client(enforce_csrf_checks=True, HTTP_USER_AGENT=FIREFOX, HTTP_ACCEPT_LANGUAGE=language)


def turkish_member() -> User:
    user = member()
    UserLanguagePreference.objects.create(user=user, language="tr")
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


def only_mail() -> EmailMultiAlternatives:
    (message,) = mail.outbox
    assert isinstance(message, EmailMultiAlternatives)
    return message


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


def assert_security_notice(message: EmailMultiAlternatives, *, language: str) -> None:
    device = "Windows üzerinde Firefox" if language == "tr" else "Firefox on Windows"
    for content in (str(message.body), html(message)):
        assert "UTC" in content
        assert device in content
        assert FIREFOX not in content
        assert "https://testserver/app/" in content


def test_signup_sends_the_verification_code_in_the_request_language() -> None:
    signup(browser("tr"))

    message = only_mail()
    assert_account_mail(
        message,
        prefix="account/email/email_confirmation_signup",
        to="owner@example.com",
        language="tr",
        subject="E-posta adresinizi doğrulay\u0131n",
    )
    assert "10 dakika" in str(message.body)


def test_reset_code_follows_the_saved_language_of_the_address() -> None:
    user = turkish_member()

    submit(browser("en"), "auth/password/request", {"email": user.email})

    assert_account_mail(
        only_mail(),
        prefix="account/email/password_reset_code",
        to=user.email,
        language="tr",
        subject="Parolan\u0131z\u0131 s\u0131f\u0131rlay\u0131n",
    )


def test_unknown_address_receives_the_product_notice() -> None:
    submit(browser("en"), "auth/password/request", {"email": "missing@example.com"})

    message = only_mail()
    assert_account_mail(
        message,
        prefix="account/email/unknown_account",
        to="missing@example.com",
        language="en",
        subject="Pyracast account not found",
    )
    assert settings.HEADLESS_FRONTEND_URLS["account_signup"] in html(message)


def test_password_change_notice_uses_the_account_language() -> None:
    user = turkish_member()
    client = signed_in(user)

    submit(
        client,
        "account/password/change",
        {"current_password": PASSWORD, "new_password": PASSWORD + "new"},
    )

    message = only_mail()
    assert_account_mail(
        message,
        prefix="account/email/password_changed",
        to=user.email,
        language="tr",
        subject="Parolan\u0131z değiştirildi",
    )
    assert_security_notice(message, language="tr")


def test_password_reset_completion_sends_a_notice() -> None:
    user = member()
    client = browser()
    submit(client, "auth/password/request", {"email": user.email})
    code = CODE.search(str(only_mail().body))
    assert code is not None
    mail.outbox.clear()

    submit(client, "auth/password/reset", {"key": code.group(1), "password": PASSWORD + "new"})

    message = only_mail()
    assert_account_mail(
        message,
        prefix="account/email/password_reset",
        to=user.email,
        language="en",
        subject="Your password was reset",
    )
    assert_security_notice(message, language="en")


def test_reauthentication_code_mail() -> None:
    user = member()
    client = signed_in(user)

    submit(client, "auth/reauthenticate/code/request", {"email": user.email})

    message = only_mail()
    assert_account_mail(
        message,
        prefix="account/email/login_code",
        to=user.email,
        language="en",
        subject="Confirm your identity",
    )
    assert "3 minutes" in str(message.body)


@pytest.mark.usefixtures("social_providers")
def test_provider_connect_notice() -> None:
    user = member()
    client = signed_in(user, reauthenticated=True)

    complete(client, begin(client, process="connect"))

    message = only_mail()
    assert_account_mail(
        message,
        prefix="socialaccount/email/account_connected",
        to=user.email,
        language="en",
        subject="A sign-in provider was connected",
    )
    assert "Google" in str(message.body)
    assert_security_notice(message, language="en")


@pytest.mark.usefixtures("social_providers")
def test_provider_disconnect_notice() -> None:
    user = member()
    account = SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    client = signed_in(user, reauthenticated=True)
    token = client.get("/api/session/").json()["csrf_token"]

    client.delete(
        AUTH + "account/providers",
        {"provider": account.provider, "account": account.uid},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )

    message = only_mail()
    assert_account_mail(
        message,
        prefix="socialaccount/email/account_disconnected",
        to=user.email,
        language="en",
        subject="A sign-in provider was disconnected",
    )
    assert_security_notice(message, language="en")


def test_recovery_code_regeneration_notice() -> None:
    client, user, _ = authenticated_account(privileged=False)
    original = RecoveryCodes.activate(user).generate_codes()
    submit_to(client, AUTH + "auth/2fa/reauthenticate", {"code": original[0]})
    mail.outbox.clear()

    submit_to(client, RECOVERY, {})

    assert_account_mail(
        only_mail(),
        prefix="mfa/email/recovery_codes_generated",
        to=user.email,
        language="en",
        subject="New recovery codes were generated",
    )


@pytest.mark.parametrize(
    ("prefix", "context", "subject", "sentence"),
    [
        ("account/email/password_set", {}, "A password was set for your account", "password"),
        (
            "account/email/email_changed",
            {"from_email": "old@example.com", "to_email": "new@example.com"},
            "Your email address was changed",
            "new@example.com",
        ),
        ("account/email/email_confirm", {}, "Your email address was verified", "verified"),
        (
            "account/email/email_deleted",
            {"deleted_email": "old@example.com"},
            "An email address was removed",
            "old@example.com",
        ),
        ("mfa/email/totp_activated", {}, "Two-factor authentication was turned on", "on"),
        ("mfa/email/totp_deactivated", {}, "Two-factor authentication was turned off", "off"),
    ],
)
def test_remaining_security_notices_render_the_product_copy(
    prefix: str, context: dict[str, str], subject: str, sentence: str
) -> None:
    user = member()
    message = AccountAdapter().render_mail(
        prefix,
        user.email,
        {
            "user": user,
            "timestamp": timezone.now(),
            "ip": "192.0.2.4",
            "user_agent": '<script>alert("unsafe")</script>',
            **context,
        },
    )

    assert isinstance(message, EmailMultiAlternatives)
    assert message.subject == subject
    assert message.extra_headers["X-Pyracast-Mail"] == prefix
    assert sentence in str(message.body)
    assert "<script>" not in html(message)
    assert "UTC" in html(message)
