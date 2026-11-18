import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal import auth as totp_auth
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, override_settings

from pyracast.accounts import audit
from pyracast.accounts.audit import LINE_KEYS, logger, redacted_key, refusal_throttle
from tests.support.admin_recovery_security import authenticated_account
from tests.support.auth_lifecycle import PASSWORD, mailed_code, member, submit, totp_code
from tests.support.company_registration import signup
from tests.support.social_accounts import (
    AUTH,
    PROVIDERS,
    begin,
    complete,
    serve_provider_profile,
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])

REQUIRED_EVENTS = (
    "login_refused",
    "session_expired",
    "password_reset_requested",
    "password_reset_code_rejected",
    "social_login_refused",
    "reauthenticated",
    "reauthentication_failed",
    "email_code_rejected",
    "email_change_requested",
    "mfa_secret_unreadable",
)
WRONG_CODE = "WRONG-CODE"
NEVER_LOGGED = (
    PASSWORD,
    WRONG_CODE,
    "member@example.com",
    "missing@example.com",
    "new@example.com",
    "operator@example.com",
    "owner@example.com",
    "solar@gmail.com",
    "google-identity",
)
Flow = Callable[[pytest.MonkeyPatch], dict[str, Any]]


@pytest.fixture
def security_log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], list[dict[str, Any]]]:
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(refusal_throttle, "_seen", OrderedDict())
    caplog.set_level(logging.INFO, logger="pyracast.security")
    return lambda: [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "pyracast.security"
    ]


def signed_in(user: User) -> Client:
    client = Client(enforce_csrf_checks=True)
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 200
    return client


def fixed_totp_clock(monkeypatch: pytest.MonkeyPatch) -> float:
    now = time.time()
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now))
    return now


@contextmanager
def social_providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    serve_provider_profile(monkeypatch)
    with override_settings(
        SOCIALACCOUNT_PROVIDERS=PROVIDERS,
    ):
        yield


def inactive_account_sign_in(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    user = member()
    User.objects.filter(pk=user.pk).update(is_active=False)
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(Client(enforce_csrf_checks=True), "auth/login", credentials).status_code == 400
    return {"user_id": user.pk, "reason": "inactive"}


def administrator_without_authenticator_sign_in(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    user = member()
    User.objects.filter(pk=user.pk).update(is_staff=True)
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(Client(enforce_csrf_checks=True), "auth/login", credentials).status_code == 400
    return {"user_id": user.pk, "reason": "mfa_not_provisioned"}


def expired_session(marker: str, value: object, reason: str) -> Flow:
    def flow(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        user = member()
        client = signed_in(user)
        session = client.session
        session[marker] = value
        session.save()
        assert client.get("/api/session/").json()["user"] is None
        return {"user_id": user.pk, "reason": reason}

    return flow


def reset_request(*, known: bool) -> Flow:
    def flow(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        user = member()
        address = user.email if known else "missing@example.com"
        response = submit(
            Client(enforce_csrf_checks=True), "auth/password/request", {"email": address}
        )
        assert response.status_code == 401
        return {"user_id": user.pk if known else None, "subject": redacted_key(address)}

    return flow


def rejected_reset_code(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    user = member()
    client = Client(enforce_csrf_checks=True)
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    rejected = submit(
        client, "auth/password/reset", {"key": WRONG_CODE, "password": PASSWORD + "new"}
    )
    assert rejected.status_code == 400
    return {"user_id": user.pk}


def administrator_without_authenticator_social_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    user = member()
    User.objects.filter(pk=user.pk).update(is_staff=True)
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    with social_providers(monkeypatch):
        client = Client()
        assert "error=permission_denied" in complete(client, begin(client))["Location"]
    return {"user_id": user.pk, "provider": "google", "reason": "mfa_not_provisioned"}


def inactive_account_social_sign_in(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    user = member()
    User.objects.filter(pk=user.pk).update(is_active=False)
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    with social_providers(monkeypatch):
        client = Client()
        response = complete(client, begin(client))
    assert "error=" in response["Location"]
    assert client.get("/api/session/").json()["user"] is None
    return {"user_id": user.pk, "provider": "google", "reason": "inactive"}


def provider_error(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    with social_providers(monkeypatch):
        client = Client()
        state = begin(client)
        response = client.get(
            "/api/accounts/google/login/callback/", {"state": state, "error": "server_error"}
        )
    assert "error=" in response["Location"]
    return {"user_id": None, "provider": "google", "reason": "provider_error"}


def password_reauthentication(*, correct: bool) -> Flow:
    def flow(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        user = member()
        client = signed_in(user)
        password = PASSWORD if correct else PASSWORD + "-wrong"
        response = submit(client, "auth/reauthenticate", {"password": password})
        assert response.status_code == (200 if correct else 400)
        return {"user_id": user.pk, "method": "password"}

    return flow


def code_reauthentication(*, correct: bool) -> Flow:
    def flow(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        user = member()
        user.set_unusable_password()
        user.save(update_fields=["password"])
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        requested = submit(client, "auth/reauthenticate/code/request", {"email": user.email})
        assert requested.status_code == 200
        code = mailed_code() if correct else WRONG_CODE
        response = submit(client, "auth/reauthenticate/code/confirm", {"code": code})
        assert response.status_code == (200 if correct else 400)
        return {"user_id": user.pk, "method": "code"}

    return flow


def authenticator_reauthentication(*, correct: bool) -> Flow:
    def flow(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        client, user, secret = authenticated_account(privileged=False)
        now = fixed_totp_clock(monkeypatch)
        code = totp_code(secret, now if correct else now - 3600)
        response = submit(client, "auth/2fa/reauthenticate", {"code": code})
        assert response.status_code == (200 if correct else 400)
        return {"user_id": user.pk, "method": "mfa", "authenticator_type": "totp"}

    return flow


def rejected_email_code(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    client = Client(enforce_csrf_checks=True)
    assert signup(client).status_code == 401
    assert submit(client, "auth/email/verify", {"key": WRONG_CODE}).status_code == 400
    return {"user_id": User.objects.get().pk}


def email_change_request(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    user = member()
    client = signed_in(user)
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    assert submit(client, "account/email", {"email": "new@example.com"}).status_code == 200
    return {"user_id": user.pk}


def unreadable_authenticator(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    user = member()
    TOTP.activate(user, generate_totp_secret())
    Authenticator.objects.filter(user=user).update(data={"secret": "mfa:v1:unreadable"})
    client = Client(enforce_csrf_checks=True)
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 401
    refused = submit(client, "auth/2fa/authenticate", {"code": "123456"})
    assert refused.status_code == 403
    assert refused.json()["code"] == "permission_denied"
    return {"user_id": user.pk}


def unreadable_recovery_codes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    client, user, secret = authenticated_account(privileged=False)
    RecoveryCodes.activate(user)
    code = totp_code(secret, fixed_totp_clock(monkeypatch))
    assert submit(client, "auth/2fa/reauthenticate", {"code": code}).status_code == 200
    Authenticator.objects.filter(user=user, type=Authenticator.Type.RECOVERY_CODES).update(
        data={"seed": "mfa:v1:unreadable", "used_mask": 0}
    )
    refused = client.get(AUTH + "account/authenticators/recovery-codes")
    assert refused.status_code == 403
    assert refused.json()["code"] == "permission_denied"
    return {"user_id": user.pk}


FLOWS: dict[str, dict[str, Flow]] = {
    "login_refused": {
        "inactive": inactive_account_sign_in,
        "mfa_not_provisioned": administrator_without_authenticator_sign_in,
    },
    "session_expired": {
        "absolute": expired_session("authenticated_at", 100.0, "absolute"),
        "idle": expired_session("last_activity_at", 100.0, "idle"),
        "invalid_marker": expired_session("authenticated_at", "invalid", "invalid_marker"),
    },
    "password_reset_requested": {
        "known": reset_request(known=True),
        "unknown": reset_request(known=False),
    },
    "password_reset_code_rejected": {"wrong_code": rejected_reset_code},
    "social_login_refused": {
        "mfa_not_provisioned": administrator_without_authenticator_social_sign_in,
        "inactive": inactive_account_social_sign_in,
        "provider_error": provider_error,
    },
    "reauthenticated": {
        "password": password_reauthentication(correct=True),
        "code": code_reauthentication(correct=True),
        "mfa": authenticator_reauthentication(correct=True),
    },
    "reauthentication_failed": {
        "password": password_reauthentication(correct=False),
        "code": code_reauthentication(correct=False),
        "mfa": authenticator_reauthentication(correct=False),
    },
    "email_code_rejected": {"wrong_code": rejected_email_code},
    "email_change_requested": {"new_address": email_change_request},
    "mfa_secret_unreadable": {
        "totp": unreadable_authenticator,
        "recovery_codes": unreadable_recovery_codes,
    },
}
CASES = [(event, case) for event in REQUIRED_EVENTS for case in FLOWS[event]]


def mailed_codes() -> set[str]:
    return {
        code
        for message in mail.outbox
        for code in re.findall(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b", str(message.body))
    }


@pytest.mark.parametrize(("event", "case"), CASES, ids=[f"{e}-{c}" for e, c in CASES])
def test_each_required_event_is_written_once_by_its_flow_as_a_redacted_line(
    event: str,
    case: str,
    security_log: Callable[[], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = FLOWS[event][case](monkeypatch)

    lines = security_log()
    [line] = [line for line in lines if line["event"] == event]
    assert {key: line.get(key) for key in expected} == expected
    assert set(line) <= LINE_KEYS
    assert line["client_ip"] == "127.0.0.1"
    text = "\n".join(json.dumps(line) for line in lines).lower()
    assert "@" not in text
    for hidden in (*NEVER_LOGGED, *mailed_codes()):
        assert hidden.lower() not in text


def test_a_password_sign_in_records_its_method(
    security_log: Callable[[], list[dict[str, Any]]],
) -> None:
    user = member()
    signed_in(user)

    assert security_log() == [
        {
            "event": "login_succeeded",
            "user_id": user.pk,
            "client_ip": "127.0.0.1",
            "method": "password",
        }
    ]


def test_the_first_verification_of_a_sign_up_records_no_address_change(
    security_log: Callable[[], list[dict[str, Any]]],
) -> None:
    client = Client(enforce_csrf_checks=True)
    assert signup(client).status_code == 401
    assert submit(client, "auth/email/verify", {"key": mailed_code()}).status_code == 200

    assert [line["event"] for line in security_log()] == [
        "signup",
        "email_verified",
        "login_succeeded",
    ]


class FailingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise RuntimeError("security sink unavailable")


def failing_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logger, "handlers", [FailingHandler()])


def failing_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(arguments: object) -> int:
        raise RuntimeError("security receiver failed")

    monkeypatch.setattr(audit, "_signal_user_id", fail)


def sign_in_succeeds(user: User) -> None:
    client = Client(enforce_csrf_checks=True)
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 200
    assert client.get("/api/session/").json()["user"] is not None


def reset_request_is_answered(user: User) -> None:
    client = Client(enforce_csrf_checks=True)
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    assert len(mail.outbox) == 1


def reauthentication_succeeds(user: User) -> None:
    client = signed_in(user)
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200


FAULTS: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "sink": failing_sink,
    "receiver": failing_receiver,
}
OUTCOMES: dict[str, Callable[[User], None]] = {
    "sign_in": sign_in_succeeds,
    "password_reset_request": reset_request_is_answered,
    "reauthentication": reauthentication_succeeds,
}


@pytest.mark.parametrize(
    ("flow", "fault"),
    [
        ("sign_in", "sink"),
        ("sign_in", "receiver"),
        ("password_reset_request", "sink"),
        ("reauthentication", "sink"),
        ("reauthentication", "receiver"),
    ],
)
def test_a_security_line_that_cannot_be_written_leaves_its_flow_unchanged(
    flow: str, fault: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = member()
    failures = logging.getLogger(audit.__name__)
    monkeypatch.setattr(failures, "handlers", [caplog.handler])
    FAULTS[fault](monkeypatch)

    OUTCOMES[flow](user)

    reports = [
        record.getMessage()
        for record in caplog.records
        if record.name == failures.name and record.levelno == logging.ERROR
    ]
    assert reports
    assert all(report.startswith("security_line_failed event=") for report in reports)
