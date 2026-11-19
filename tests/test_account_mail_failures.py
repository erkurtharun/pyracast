import json
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from smtplib import SMTPRecipientsRefused
from typing import Any, ClassVar

import pytest
import requests
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend
from django.test import Client, override_settings

from pyracast.accounts.models import Membership, Tenant
from pyracast.mail.errors import NotSentError
from tests.support.admin_recovery_security import RECOVERY, authenticated_account
from tests.support.admin_recovery_security import submit as submit_to
from tests.support.auth_lifecycle import PASSWORD, member, submit
from tests.support.company_registration import signup
from tests.support.social_accounts import CALLBACK, PROVIDERS, begin, complete

pytestmark = [
    pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"]),
    pytest.mark.usefixtures("failing_mail"),
]
AUTH = "/api/auth/browser/v1/"
CODE = re.compile(r"[A-Z0-9]{4}-[A-Z0-9]{4}")


def smtp_refusal(message: EmailMessage) -> Exception:
    return SMTPRecipientsRefused(
        {address: (550, b"5.1.1 mailbox unavailable") for address in message.to}
    )


def gmail_rejection(message: EmailMessage) -> Exception:
    return NotSentError(f"Gmail rejected the message for {', '.join(message.to)}")


def relay_timeout(message: EmailMessage) -> Exception:
    return TimeoutError(f"timed out reaching the relay for {', '.join(message.to)}")


class FailingBackend(BaseEmailBackend):
    failure: ClassVar[Callable[[EmailMessage], Exception] | None] = None
    attempted: ClassVar[list[EmailMessage]] = []
    raised: ClassVar[list[Exception]] = []

    def send_messages(self, email_messages: Sequence[EmailMessage] | None) -> int:
        assert email_messages is not None
        self.attempted.extend(email_messages)
        failure = type(self).failure
        if failure is not None:
            error = failure(email_messages[0])
            self.raised.append(error)
            raise error
        return len(email_messages)


@pytest.fixture(
    params=[smtp_refusal, gmail_rejection, relay_timeout],
    ids=["smtp", "gmail", "timeout"],
)
def failing_mail(request: pytest.FixtureRequest) -> Iterator[None]:
    FailingBackend.failure = request.param
    FailingBackend.attempted, FailingBackend.raised = [], []
    with override_settings(
        MAILERS={"default": {"BACKEND": "tests.test_account_mail_failures.FailingBackend"}}
    ):
        yield
    FailingBackend.failure = None


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
    ):
        yield


def attempted_code() -> str:
    for message in reversed(FailingBackend.attempted):
        for line in str(message.body).splitlines():
            if CODE.fullmatch(line.strip()):
                return line.strip()
    raise AssertionError("No authentication code was attempted.")


def assert_no_failure_detail(response: Any) -> None:
    for error in FailingBackend.raised:
        assert type(error).__name__.encode() not in response.content
        assert str(error).encode() not in response.content
    assert b"mailbox unavailable" not in response.content
    assert b"Gmail rejected" not in response.content
    assert b"timed out" not in response.content


def mail_failure_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if "account_mail_failed" in record.getMessage()]


def signed_in(user: User, *, reauthenticated: bool = False) -> Client:
    client = Client(enforce_csrf_checks=True)
    assert (
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200
    )
    if reauthenticated:
        assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    return client


def test_signup_with_failing_mail_reaches_verification_stage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = Client(enforce_csrf_checks=True)

    response = signup(client)

    assert response.status_code == 401
    assert any(flow["id"] == "verify_email" for flow in response.json()["data"]["flows"])
    assert_no_failure_detail(response)
    user = User.objects.get(email="owner@example.com")
    assert Membership.objects.get(user=user).tenant == Tenant.objects.get()
    assert not EmailAddress.objects.get(user=user).verified
    assert [message.to for message in FailingBackend.attempted] == [["owner@example.com"]]
    [record] = mail_failure_records(caplog)
    assert f" user_id={user.pk} " in record.getMessage()
    assert user.email not in record.getMessage()
    assert f"={user.get_username()} " not in record.getMessage()

    FailingBackend.failure = None
    cache.clear()
    assert submit(client, "auth/email/verify/resend", {}).status_code == 200
    assert submit(client, "auth/email/verify", {"key": attempted_code()}).status_code == 200
    assert EmailAddress.objects.get(user=user).verified


def test_password_reset_request_with_failing_mail_answers_like_success() -> None:
    user = member()
    reset = Client(enforce_csrf_checks=True)

    known = submit(reset, "auth/password/request", {"email": user.email})
    code = attempted_code()
    unknown = submit(
        Client(enforce_csrf_checks=True), "auth/password/request", {"email": "missing@example.com"}
    )

    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json()
    assert_no_failure_detail(known)
    assert_no_failure_detail(unknown)
    assert [message.to for message in FailingBackend.attempted] == [
        [user.email],
        ["missing@example.com"],
    ]
    changed = submit(reset, "auth/password/reset", {"key": code, "password": PASSWORD + "new"})
    assert changed.status_code == 401
    user.refresh_from_db()
    assert user.check_password(PASSWORD + "new")


def test_password_change_with_failing_mail_completes_and_is_audited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = member()
    client = signed_in(user)

    with caplog.at_level("INFO", logger="pyracast.security"):
        changed = submit(
            client,
            "account/password/change",
            {"current_password": PASSWORD, "new_password": PASSWORD + "new"},
        )

    assert changed.status_code == 401
    assert changed.json()["meta"]["password_changed"] is True
    assert_no_failure_detail(changed)
    user.refresh_from_db()
    assert user.check_password(PASSWORD + "new")
    assert '"event": "password_changed"' in caplog.text
    assert client.get("/api/session/").json()["user"] is None
    assert len(FailingBackend.attempted) == 1


@pytest.mark.usefixtures("social_providers")
def test_provider_connect_with_failing_mail_redirects_to_callback() -> None:
    user = member()
    client = signed_in(user, reauthenticated=True)

    response = complete(client, begin(client, process="connect"))

    assert response.status_code == 302
    assert response["Location"] == CALLBACK
    assert SocialAccount.objects.get().user_id == user.pk
    assert len(FailingBackend.attempted) == 1


@pytest.mark.usefixtures("social_providers")
def test_provider_disconnect_with_failing_mail_answers_200() -> None:
    user = member()
    account = SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    client = signed_in(user, reauthenticated=True)
    token = client.get("/api/session/").json()["csrf_token"]

    removed = client.delete(
        AUTH + "account/providers",
        {"provider": account.provider, "account": account.uid},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )

    assert removed.status_code == 200
    assert removed.json()["data"] == []
    assert_no_failure_detail(removed)
    assert not SocialAccount.objects.exists()
    assert len(FailingBackend.attempted) == 1


def test_recovery_code_regeneration_with_failing_mail_returns_codes() -> None:
    client, user, _ = authenticated_account(privileged=False)
    original = RecoveryCodes.activate(user).generate_codes()
    reauthenticated = submit_to(client, AUTH + "auth/2fa/reauthenticate", {"code": original[0]})
    assert reauthenticated.status_code == 200
    assert FailingBackend.attempted == []

    response = submit_to(client, RECOVERY, {})

    assert response.status_code == 200
    codes = response.json()["data"]["unused_codes"]
    assert codes
    assert not set(codes) & set(original)
    assert_no_failure_detail(response)
    stored = Authenticator.objects.get(user=user, type=Authenticator.Type.RECOVERY_CODES)
    assert RecoveryCodes(stored).get_unused_codes() == codes
    assert len(FailingBackend.attempted) == 1


def test_account_mail_failure_is_logged_once_without_address_or_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = member()
    client = signed_in(user)

    with caplog.at_level("INFO", logger="pyracast"):
        response = submit(client, "auth/reauthenticate/code/request", {"email": user.email})

    assert response.status_code == 200
    assert_no_failure_detail(response)
    code = attempted_code()
    [record] = mail_failure_records(caplog)
    assert record.name == "pyracast.accounts.adapters"
    assert record.levelname == "ERROR"
    assert record.exc_info is None
    error = FailingBackend.raised[-1]
    assert "template=account/email/login_code" in record.getMessage()
    assert f"error_type={type(error).__name__}" in record.getMessage()
    expected_code = error.code if isinstance(error, NotSentError) else None
    assert record.getMessage().endswith(f" error_code={expected_code}")
    assert user.email not in caplog.text
    assert code not in caplog.text
    assert str(error) not in caplog.text
    assert "mailbox unavailable" not in caplog.text
    assert submit(client, "auth/reauthenticate/code/confirm", {"code": code}).status_code == 200
