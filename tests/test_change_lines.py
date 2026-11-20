import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.socialaccount.models import SocialAccount
from allauth.usersessions.models import UserSession
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.db import DatabaseError, transaction
from django.test import Client, override_settings

from pyracast.accounts import adapters, processes
from pyracast.accounts.audit import failure_logger, logger, refusal_throttle
from pyracast.accounts.models import Membership, Tenant, WorkspaceInvitation, WorkspaceRole
from pyracast.notifications import intents
from pyracast.notifications.models import OutboundMail, OutboundMailKind
from tests.support.account_mail import one_minute_later
from tests.support.account_sessions import (
    account,
    browser,
    capture_security_events,
    session_key,
    session_row_held,
    session_user,
    signed_in,
    totp_clock,
    with_totp,
)
from tests.support.auth_lifecycle import AUTH, PASSWORD, mailed_code, submit, totp_code
from tests.support.company_registration import signup
from tests.support.outbox import deliver_mail
from tests.support.social_accounts import (
    CALLBACK,
    PROVIDERS,
    begin,
    callback,
    finish,
    serve_provider_profile,
)

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
NEW_PASSWORD = PASSWORD + "-new"


@pytest.fixture(autouse=True)
def providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    serve_provider_profile(monkeypatch)
    with override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS):
        yield


def stored() -> tuple[list[Any], ...]:
    # What an account flow writes; session rows are left out, since an answer below 500 saves
    # its session after the transaction whatever the transaction did.
    return (
        list(
            User.objects.order_by("pk").values_list(
                "pk", "password", "email", "is_active", "last_login"
            )
        ),
        list(
            EmailAddress.objects.order_by("pk").values_list(
                "user_id", "email", "verified", "primary"
            )
        ),
        list(
            Authenticator.objects.order_by("pk").values_list("pk", "type", "data", "last_used_at")
        ),
        list(SocialAccount.objects.order_by("pk").values_list("user_id", "provider", "uid")),
        sorted(UserSession.objects.values_list("session_key", flat=True)),
        list(Tenant.objects.order_by("pk").values_list("pk", "name")),
        list(WorkspaceInvitation.objects.order_by("pk").values_list("pk", "email", "revoked_at")),
        list(OutboundMail.objects.order_by("pk").values_list("pk", "kind")),
    )


def prepared(client: Client, path: str, data: dict[str, Any]) -> Callable[[], Any]:
    # The token is read first, so the attempt is the only request that meets its fault.
    token = client.get("/api/session/").json()["csrf_token"]
    client.raise_request_exception = False
    return lambda: client.post(
        AUTH + path, data, content_type="application/json", HTTP_X_CSRFTOKEN=token
    )


def failing_notice(kind: str) -> Callable[[], AbstractContextManager[None]]:
    @contextmanager
    def failing() -> Iterator[None]:
        store = intents.enqueue_mail

        def refused(mail_kind: OutboundMailKind, *args: Any, **kwargs: Any) -> None:
            if mail_kind == kind:
                raise DatabaseError("The notice could not be stored.")
            store(mail_kind, *args, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(adapters, "enqueue_mail", refused)
            yield

    return failing


@contextmanager
def failing_after_the_start() -> Iterator[None]:
    def failed(*_args: Any) -> None:
        raise DatabaseError("The transaction failed after the work it started.")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(processes, "stamp_started", failed)
        yield


@dataclass(frozen=True)
class Attempt:
    send: Callable[[], Any]
    # Makes the attempt's transaction roll back after it wrote its change lines.
    failing: Callable[[], AbstractContextManager[object]]
    # The change lines of the attempt when its transaction commits, in order.
    lines: list[str]
    rolled_back: int


def address_change(_monkeypatch: pytest.MonkeyPatch) -> Attempt:
    user = account("changer")
    signed_in(user)
    client = signed_in(user, reauthenticated=True)
    assert submit(client, "account/email", {"email": "moved@example.com"}).status_code == 200
    code = mailed_code()
    return Attempt(
        prepared(client, "auth/email/verify", {"key": code}),
        failing_notice("account/email/email_changed"),
        ["email_added", "email_verified", "email_changed", "sessions_revoked"],
        500,
    )


def signup_verification(_monkeypatch: pytest.MonkeyPatch) -> Attempt:
    client = browser()
    assert signup(client).status_code == 401
    code = mailed_code()
    return Attempt(
        prepared(client, "auth/email/verify", {"key": code}),
        lambda: session_row_held(session_key(client)),
        ["email_verified", "login_succeeded"],
        503,
    )


def password_reset(_monkeypatch: pytest.MonkeyPatch) -> Attempt:
    user = account("forgetful")
    signed_in(user)
    client = browser()
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    code = mailed_code()
    return Attempt(
        prepared(client, "auth/password/reset", {"key": code, "password": NEW_PASSWORD}),
        failing_notice("account/email/password_reset"),
        ["password_reset", "sessions_revoked"],
        500,
    )


def address_change_resend(monkeypatch: pytest.MonkeyPatch) -> Attempt:
    user = account("resender")
    client = signed_in(user, reauthenticated=True)
    assert submit(client, "account/email", {"email": "moved@example.com"}).status_code == 200
    deliver_mail()
    one_minute_later(monkeypatch)

    @contextmanager
    def changed_before_the_check() -> Iterator[None]:
        # The account's state changes after the code is queued, before the resend's check.
        store = intents.enqueue_mail

        def stored_then_changed(*args: Any, **kwargs: Any) -> None:
            store(*args, **kwargs)
            User.objects.filter(pk=user.pk).update(password=make_password(NEW_PASSWORD))

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(adapters, "enqueue_mail", stored_then_changed)
            yield

    return Attempt(
        prepared(client, "auth/email/verify/resend", {}),
        changed_before_the_check,
        ["email_change_requested"],
        409,
    )


def company_signup(_monkeypatch: pytest.MonkeyPatch) -> Attempt:
    client = browser()
    client.raise_request_exception = False
    return Attempt(
        lambda: signup(client),
        failing_notice("account/email/email_confirmation_signup"),
        ["signup"],
        500,
    )


def provider_signup(_monkeypatch: pytest.MonkeyPatch) -> Attempt:
    client = browser()
    assert callback(client, begin(client))["Location"] == CALLBACK
    finish(client)
    details = {"email": "typed@example.com", "company_name": "Solar"}
    return Attempt(
        prepared(client, "auth/provider/signup", details),
        failing_notice("account/email/email_confirmation_signup"),
        ["signup"],
        500,
    )


def second_factor_step(monkeypatch: pytest.MonkeyPatch) -> Attempt:
    now = time.time()
    totp_clock(monkeypatch, now)
    user = account("guarded")
    secret = with_totp(user)
    client = browser()
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 401
    return Attempt(
        prepared(client, "auth/2fa/authenticate", {"code": totp_code(secret, now)}),
        lambda: session_row_held(session_key(client)),
        ["mfa_succeeded", "login_succeeded"],
        503,
    )


def password_sign_in(_monkeypatch: pytest.MonkeyPatch) -> Attempt:
    user = account("returning")
    client = browser()
    credentials = {"email": user.email, "password": PASSWORD}
    return Attempt(
        prepared(client, "auth/login", credentials),
        failing_after_the_start,
        ["login_succeeded"],
        500,
    )


ATTEMPTS: dict[str, Callable[[pytest.MonkeyPatch], Attempt]] = {
    "address-change-verification": address_change,
    "signup-verification": signup_verification,
    "password-reset": password_reset,
    "address-change-resend": address_change_resend,
    "company-signup": company_signup,
    "provider-signup": provider_signup,
    "second-factor-step": second_factor_step,
    "password-sign-in": password_sign_in,
}


@pytest.mark.parametrize("flow", ATTEMPTS)
def test_an_attempt_rolled_back_after_its_change_lines_writes_none_and_changes_nothing(
    flow: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempt = ATTEMPTS[flow](monkeypatch)
    before = stored()
    events = capture_security_events(caplog, monkeypatch)
    written = len(events())

    with attempt.failing():
        response = attempt.send()

    assert response.status_code == attempt.rolled_back
    assert events()[written:] == []
    assert stored() == before


@pytest.mark.parametrize("flow", ATTEMPTS)
def test_an_attempt_that_commits_writes_its_change_lines_once_in_order(
    flow: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempt = ATTEMPTS[flow](monkeypatch)
    before = stored()
    events = capture_security_events(caplog, monkeypatch)
    written = len(events())

    response = attempt.send()

    assert response.status_code < 500
    assert [line["event"] for line in events()[written:]] == attempt.lines
    assert stored() != before


class RolledBackError(Exception):
    pass


def test_a_connection_refused_for_its_reauthentication_writes_its_refusal_as_it_rolls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(refusal_throttle, "_seen", OrderedDict())
    user = account("connecting")
    client = signed_in(user, reauthenticated=True)
    state = begin(client, process="connect")
    session = client.session
    records = session["account_authentication_methods"]
    for record in records:
        record["at"] = time.time() - 600
    session["account_authentication_methods"] = records
    session.save()
    before = stored()
    events = capture_security_events(caplog, monkeypatch)
    written = len(events())

    assert callback(client, state)["Location"] == CALLBACK
    response = finish(client)

    assert response.status_code == 401
    assert [(line["event"], line.get("reason")) for line in events()[written:]] == [
        ("social_login_refused", "reauthentication_required")
    ]
    assert stored() == before


def test_an_invitation_refused_by_its_budget_writes_its_refusal_as_it_rolls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tenant = Tenant.objects.create(name="budget")
    administrator = User.objects.create_user(username="budget-admin", email="admin@example.com")
    Membership.objects.create(tenant=tenant, user=administrator, role=WorkspaceRole.ADMIN)
    client = browser()
    client.force_login(administrator)
    token = client.get("/api/session/").json()["csrf_token"]

    def invited() -> Any:
        return client.post(
            f"/api/tenants/{tenant.pk}/invitations/",
            {"email": "guest@example.com"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )

    assert invited().status_code == 201
    before = stored()
    events = capture_security_events(caplog, monkeypatch)
    written = len(events())

    response = invited()

    assert response.status_code == 429
    assert [(line["event"], line.get("reason")) for line in events()[written:]] == [
        ("invitation_limited", "resend")
    ]
    assert stored() == before


def test_a_wrong_second_factor_code_writes_its_refusal_in_a_transaction_that_rolls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = time.time()
    totp_clock(monkeypatch, now)
    user = account("mistyped")
    secret = with_totp(user)
    client = browser()
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 401
    wrong = f"{(int(totp_code(secret, now)) + 1) % 1_000_000:06d}"
    send = prepared(client, "auth/2fa/authenticate", {"code": wrong})
    events = capture_security_events(caplog, monkeypatch)
    written = len(events())
    answers: list[Any] = []

    # No product transaction rolls back after a wrong code, so the test opens one around it.
    with suppress(RolledBackError), transaction.atomic():
        answers.append(send())
        raise RolledBackError

    assert [response.status_code for response in answers] == [400]
    assert [line["event"] for line in events()[written:]] == ["mfa_failed"]


class FailingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise RuntimeError("security sink unavailable")


def test_change_lines_that_cannot_be_written_at_the_commit_change_nothing_else(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    user = account("rotating")
    other = signed_in(user)
    client = signed_in(user, reauthenticated=True)
    change = prepared(
        client,
        "account/password/change",
        {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    commit_hooks = logging.getLogger("django.db.backends.base")
    for reporter in (failure_logger, commit_hooks):
        monkeypatch.setattr(reporter, "handlers", [caplog.handler])
        monkeypatch.setattr(reporter, "propagate", False)
    monkeypatch.setattr(logger, "handlers", [FailingHandler()])
    caplog.set_level(logging.INFO)
    reported = len(caplog.records)

    response = change()

    assert response.status_code == 401
    assert response.json()["meta"]["password_changed"] is True
    assert [
        record.getMessage()
        for record in caplog.records[reported:]
        if record.levelno >= logging.ERROR
    ] == [
        "security_line_failed event=logout error_type=RuntimeError",
        "security_line_failed event=password_changed error_type=RuntimeError",
        "security_line_failed event=sessions_revoked error_type=RuntimeError",
    ]
    assert User.objects.get(pk=user.pk).check_password(NEW_PASSWORD)
    assert session_user(other) is None


@pytest.mark.parametrize("pending", ["signup", "address-change"])
def test_a_verification_resend_whose_session_cannot_be_saved_queues_no_code_and_writes_no_line(
    pending: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    if pending == "signup":
        client = browser()
        assert signup(client).status_code == 401
    else:
        client = signed_in(account("resender"), reauthenticated=True)
        assert submit(client, "account/email", {"email": "moved@example.com"}).status_code == 200
    deliver_mail()
    one_minute_later(monkeypatch)
    resend = prepared(client, "auth/email/verify/resend", {})
    before = stored()
    events = capture_security_events(caplog, monkeypatch)
    written = len(events())

    with session_row_held(session_key(client)):
        response = resend()

    assert stored() == before
    assert events()[written:] == []
    assert (response.status_code, response.json()["code"]) == (503, "busy")
