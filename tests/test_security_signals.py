import ast
import json
import logging
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from allauth.account import signals as account_signals
from allauth.account.models import EmailAddress, EmailConfirmation
from allauth.mfa import signals as mfa_signals
from allauth.mfa.models import Authenticator
from allauth.socialaccount import signals as social_signals
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import signals as auth_signals
from django.contrib.auth.models import User
from django.db import transaction
from django.dispatch import Signal
from django.test import RequestFactory

from pyracast.accounts.audit import (
    CHANGE_EVENTS,
    LINE_KEYS,
    SECURITY_EVENT_DETAILS,
    SIGNAL_EVENTS,
    RefusalThrottle,
    SecurityEvent,
    connect_security_events,
    failure_logger,
    logger,
    record_on_commit,
    record_security_event,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "pyracast"
PINNED_LINE_KEYS = frozenset(
    {
        "event",
        "user_id",
        "client_ip",
        "subject",
        "tenant_id",
        "target_id",
        "reason",
        "view",
        "method",
        "authenticator_type",
        "provider",
        "scope",
        "count",
    }
)
WRITERS = frozenset({"record_security_event", "record_on_commit", "record_refusal"})
LINE_ARGUMENTS = frozenset({"request", "user_id", "client_ip"})
DETAIL_OF_ARGUMENT = {"address": "subject"}
SIGNALLED = frozenset(
    {
        "login_succeeded",
        "logout",
        "login_failed",
        "login_code_rejected",
        "signup",
        "email_verified",
        "email_code_rejected",
        "email_added",
        "email_changed",
        "email_removed",
        "password_changed",
        "password_set",
        "password_reset",
        "password_reset_code_rejected",
        "mfa_added",
        "mfa_removed",
        "mfa_reset",
        "mfa_succeeded",
        "mfa_failed",
        "social_account_connected",
        "social_account_disconnected",
        "reauthenticated",
        "email_change_requested",
    }
)
WRITTEN = frozenset(
    {
        "login_refused",
        "session_expired",
        "sessions_revoked",
        "password_reset_requested",
        "social_login_refused",
        "reauthentication_failed",
        "mfa_secret_unreadable",
        "login_locked",
        "password_change_failed",
        "second_factor_locked",
        "admin_mfa_provisioned",
        "mfa_reset_by_operator",
        "invalid_proxy_headers",
        "authentication_request_throttled",
        "api_throttled",
        "authorization_denied",
        "csrf_refused",
        "invitation_sent",
        "invitation_limited",
        "invitation_revoked",
        "invitation_accepted",
        "invitation_denied",
        "workspace_member_removed",
        "workspace_admin_reassigned",
    }
)
CHANGES = frozenset(
    {
        "login_succeeded",
        "logout",
        "sessions_revoked",
        "reauthenticated",
        "mfa_succeeded",
        "signup",
        "email_verified",
        "email_added",
        "email_change_requested",
        "email_changed",
        "email_removed",
        "password_changed",
        "password_set",
        "password_reset",
        "mfa_added",
        "mfa_removed",
        "mfa_reset",
        "social_account_connected",
        "social_account_disconnected",
        "admin_mfa_provisioned",
        "mfa_reset_by_operator",
        "invitation_sent",
        "invitation_revoked",
        "invitation_accepted",
        "workspace_member_removed",
        "workspace_admin_reassigned",
    }
)
ADDRESS = "Private.Person@Example.com"
HIDDEN = "test-only-hidden-value"
USER_ID = 4821


class RolledBackError(Exception):
    pass


@pytest.fixture(autouse=True)
def security_log(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    caplog.set_level(logging.INFO, logger="pyracast.security")
    connect_security_events()


def user() -> User:
    return User(pk=USER_ID, username="private-person", email=ADDRESS)


def address() -> EmailAddress:
    return EmailAddress(user_id=USER_ID, email=ADDRESS)


def social_account() -> SocialAccount:
    return SocialAccount(user_id=USER_ID, provider="google", uid=HIDDEN, extra_data={"e": ADDRESS})


def authenticator() -> Authenticator:
    return Authenticator(user_id=USER_ID, type=Authenticator.Type.TOTP, data={"secret": HIDDEN})


def arguments(signal: Signal) -> dict[str, Any]:
    by_user = {"user": user()}
    with_address = {"user": user(), "email_address": address()}
    with_factor = {"user": user(), "authenticator": authenticator()}
    rejected_code = {"user": user(), "last_attempt": False}
    table: dict[Signal, dict[str, Any]] = {
        auth_signals.user_logged_in: by_user,
        auth_signals.user_logged_out: by_user,
        auth_signals.user_login_failed: {
            "credentials": {"email": ADDRESS, "password": HIDDEN, "token": HIDDEN}
        },
        account_signals.user_signed_up: by_user,
        account_signals.email_confirmed: {"email_address": address()},
        account_signals.email_added: with_address,
        account_signals.email_removed: with_address,
        account_signals.email_changed: {
            "user": user(),
            "from_email_address": EmailAddress(user_id=USER_ID, email=f"old.{ADDRESS}"),
            "to_email_address": address(),
        },
        account_signals.password_changed: by_user,
        account_signals.password_set: by_user,
        account_signals.password_reset: by_user,
        account_signals.login_code_rejected: rejected_code,
        account_signals.password_reset_code_rejected: rejected_code,
        account_signals.email_verification_code_rejected: rejected_code,
        account_signals.authentication_step_completed: {
            "user": user(),
            "method": "password",
            "email": ADDRESS,
            "reauthenticated": True,
        },
        account_signals.email_confirmation_sent: {
            "confirmation": EmailConfirmation(email_address=address(), key=HIDDEN),
            "signup": False,
        },
        mfa_signals.authenticator_added: with_factor,
        mfa_signals.authenticator_removed: with_factor,
        mfa_signals.authenticator_reset: with_factor,
        mfa_signals.authenticator_used: with_factor | {"reauthenticated": False},
        mfa_signals.authentication_failed: with_factor,
        social_signals.social_account_added: {
            "sociallogin": SocialLogin(user=user(), account=social_account())
        },
        social_signals.social_account_removed: {"socialaccount": social_account()},
    }
    return table[signal]


def lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(record.message) for record in caplog.records]


def send_to_security_receivers(
    signal: Signal, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> None:
    # Session and last-login receivers need a database; only the security receivers run here.
    monkeypatch.setattr(
        signal,
        "receivers",
        [entry for entry in signal.receivers if str(entry[0][0]).startswith("pyracast.security.")],
    )
    signal.send(sender=User, **kwargs)


@pytest.mark.parametrize(
    ("signal", "event"), SIGNAL_EVENTS.items(), ids=list(SIGNAL_EVENTS.values())
)
def test_every_signal_writes_one_redacted_line(
    signal: Signal,
    event: SecurityEvent,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RequestFactory().post("/api/auth/browser/v1/auth/login")
    send_to_security_receivers(signal, monkeypatch, request=request, **arguments(signal))

    [line] = lines(caplog)
    assert line["event"] == event
    assert set(line) <= LINE_KEYS
    assert line["client_ip"] == "127.0.0.1"
    assert line["user_id"] == (None if signal is auth_signals.user_login_failed else USER_ID)
    assert ADDRESS.lower() not in caplog.text.lower()
    assert HIDDEN not in caplog.text
    assert "private-person" not in caplog.text


@pytest.mark.parametrize(
    ("signal", "kwargs"),
    [
        (
            account_signals.email_changed,
            {"user": user(), "from_email_address": None, "to_email_address": address()},
        ),
        (
            account_signals.email_confirmation_sent,
            {
                "confirmation": EmailConfirmation(email_address=address(), key=HIDDEN),
                "signup": True,
            },
        ),
        (
            account_signals.authentication_step_completed,
            {"user": user(), "method": "password", "email": ADDRESS},
        ),
    ],
    ids=["first_address", "signup_code", "sign_in_step"],
)
def test_signals_that_describe_no_security_change_write_nothing(
    signal: Signal,
    kwargs: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RequestFactory().post("/api/auth/browser/v1/auth/login")
    send_to_security_receivers(signal, monkeypatch, request=request, **kwargs)

    assert lines(caplog) == []


def writer_calls() -> list[tuple[str, ast.expr | None]]:
    calls = []
    for path in SOURCE.rglob("*.py"):
        if path.name == "audit.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in WRITERS
            ):
                calls.append(
                    (f"{path.relative_to(SOURCE)}:{node.lineno}", next(iter(node.args), None))
                )
    return calls


def named_events(argument: ast.expr | None) -> set[str]:
    if argument is None:
        return set()
    return {
        node.attr
        for node in ast.walk(argument)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "SecurityEvent"
    }


def test_the_vocabulary_and_line_schema_are_the_pinned_ones() -> None:
    assert {event.value for event in SecurityEvent} == SIGNALLED | WRITTEN
    assert {event.value for event in SIGNAL_EVENTS.values()} == SIGNALLED
    assert len(SIGNAL_EVENTS) == len(SIGNALLED)
    assert PINNED_LINE_KEYS == LINE_KEYS


def test_every_written_event_has_a_writer_call_and_every_writer_call_names_one() -> None:
    calls = writer_calls()
    named = {location: named_events(argument) for location, argument in calls}

    assert calls
    assert [location for location, events in named.items() if not events] == []
    assert {SecurityEvent[name].value for events in named.values() for name in events} == WRITTEN


def written_details() -> list[tuple[str, set[str], set[str]]]:
    found = []
    for path in SOURCE.rglob("*.py"):
        if path.name == "audit.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in WRITERS
            ):
                arguments = {keyword.arg for keyword in node.keywords if keyword.arg}
                found.append(
                    (
                        f"{path.relative_to(SOURCE)}:{node.lineno}",
                        named_events(next(iter(node.args), None)),
                        {DETAIL_OF_ARGUMENT.get(name, name) for name in arguments - LINE_ARGUMENTS},
                    )
                )
    return found


def test_every_writer_call_passes_only_the_details_its_events_declare() -> None:
    calls = written_details()
    undeclared = {
        f"{location} {name}": sorted(details - SECURITY_EVENT_DETAILS[SecurityEvent[name]])
        for location, events, details in calls
        for name in events
        if details - SECURITY_EVENT_DETAILS[SecurityEvent[name]]
    }

    assert calls
    assert undeclared == {}
    assert set().union(*SECURITY_EVENT_DETAILS.values()) <= LINE_KEYS


def reported_failures(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(failure_logger, "handlers", [caplog.handler])
    monkeypatch.setattr(failure_logger, "propagate", False)


def test_a_detail_its_event_does_not_declare_is_refused_and_reported(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    reported_failures(caplog, monkeypatch)

    record_security_event(SecurityEvent.LOGOUT, user_id=USER_ID, tenant_id=7)

    assert [record.getMessage() for record in caplog.records] == [
        "security_line_failed event=logout error_type=ValueError"
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"tenant_id": "wrong-type", "target_id": 3},
        {"tenant_id": 7, "target_id": {"unexpected": "object"}},
        {"tenant_id": True, "target_id": 3},
        {"tenant_id": 7, "target_id": 3, "user_id": "4821"},
        {"tenant_id": 7, "target_id": 3, "client_ip": 127},
    ],
    ids=["text_tenant", "object_target", "flag_tenant", "text_user", "number_client_ip"],
)
def test_a_detail_of_another_type_is_refused_and_reported(
    fields: dict[str, Any], caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    reported_failures(caplog, monkeypatch)

    record_security_event(SecurityEvent.INVITATION_ACCEPTED, **fields)

    assert [record.getMessage() for record in caplog.records] == [
        "security_line_failed event=invitation_accepted error_type=ValueError"
    ]


def test_a_request_budget_scope_must_be_text(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    reported_failures(caplog, monkeypatch)
    fields: dict[str, Any] = {"scope": 7}

    record_security_event(SecurityEvent("api_throttled"), user_id=USER_ID, **fields)

    assert [record.getMessage() for record in caplog.records] == [
        "security_line_failed event=api_throttled error_type=ValueError"
    ]


def test_failures_for_one_address_share_a_subject_that_is_not_the_address(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = RequestFactory().post("/api/auth/browser/v1/auth/login")
    for identifier in (ADDRESS, f"  {ADDRESS.upper()} ", "other@example.com"):
        send_to_security_receivers(
            auth_signals.user_login_failed,
            monkeypatch,
            request=request,
            credentials={"email": identifier, "password": HIDDEN},
        )

    first, second, other = (line["subject"] for line in lines(caplog))
    assert first == second != other
    assert re.fullmatch(r"[0-9a-f]{16}", first)


def test_the_security_alerts_watch_events_the_application_writes() -> None:
    monitoring = (ROOT / "infra" / "terraform" / "monitoring.tf").read_text(encoding="utf-8")
    watched = set(re.findall(r'signal\s*=\s*"([a-z_]+)"', monitoring))

    assert watched
    assert watched <= {event.value for event in SecurityEvent}


def test_one_email_confirmation_carries_no_key_into_the_line(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    confirmation = EmailConfirmation(email_address=address(), key=HIDDEN)
    send_to_security_receivers(
        account_signals.email_confirmed,
        monkeypatch,
        request=RequestFactory().get("/"),
        email_address=address(),
        confirmation=confirmation,
    )
    assert lines(caplog) == [
        {"event": "email_verified", "user_id": USER_ID, "client_ip": "127.0.0.1"}
    ]
    assert HIDDEN not in caplog.text


def test_the_refusal_throttle_admits_a_key_once_per_interval_and_forgets_the_oldest() -> None:
    throttle = RefusalThrottle()
    throttle.capacity = 2

    assert throttle.admit((1, "view", "code"))
    assert not throttle.admit((1, "view", "code"))
    assert throttle.admit((2, "view", "code"))
    assert throttle.admit((3, "view", "code"))
    assert throttle.admit((1, "view", "code"))


def test_the_refusal_throttle_admits_a_key_again_after_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    throttle = RefusalThrottle()

    assert throttle.admit((1, "view", "code"))
    now[0] += throttle.interval - 0.5
    assert not throttle.admit((1, "view", "code"))
    now[0] += 0.5
    assert throttle.admit((1, "view", "code"))


@pytest.mark.django_db(transaction=True)
def test_a_line_bound_to_a_commit_is_written_only_when_the_transaction_commits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def rolled_back() -> None:
        with transaction.atomic():
            record_on_commit(SecurityEvent.INVITATION_SENT, user_id=USER_ID, target_id=1)
            raise RuntimeError

    with pytest.raises(RuntimeError):
        rolled_back()
    with transaction.atomic():
        record_on_commit(SecurityEvent.INVITATION_SENT, user_id=USER_ID, target_id=2)

    assert lines(caplog) == [
        {"event": "invitation_sent", "user_id": USER_ID, "client_ip": None, "target_id": 2}
    ]


def test_the_change_lines_are_the_pinned_ones() -> None:
    assert {event.value for event in CHANGE_EVENTS} == CHANGES


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("event", sorted(CHANGES))
def test_a_change_line_leaves_only_with_the_commit_of_its_transaction(
    event: str, caplog: pytest.LogCaptureFixture
) -> None:
    with suppress(RolledBackError), transaction.atomic():
        record_security_event(SecurityEvent(event), user_id=USER_ID)
        raise RolledBackError
    with transaction.atomic():
        record_security_event(SecurityEvent(event), user_id=USER_ID)
        assert lines(caplog) == []

    assert lines(caplog) == [{"event": event, "user_id": USER_ID, "client_ip": None}]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("event", sorted({event.value for event in SecurityEvent} - CHANGES))
def test_any_other_line_is_written_at_once_also_in_a_transaction_that_rolls_back(
    event: str, caplog: pytest.LogCaptureFixture
) -> None:
    with suppress(RolledBackError), transaction.atomic():
        record_security_event(SecurityEvent(event), user_id=USER_ID)
        assert len(lines(caplog)) == 1
        raise RolledBackError

    assert lines(caplog) == [{"event": event, "user_id": USER_ID, "client_ip": None}]


@pytest.mark.django_db(transaction=True)
def test_a_savepoint_that_rolls_back_drops_only_its_own_change_lines(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with transaction.atomic():
        record_security_event(SecurityEvent.EMAIL_ADDED, user_id=USER_ID)
        with suppress(RolledBackError), transaction.atomic():
            record_security_event(SecurityEvent.EMAIL_REMOVED, user_id=USER_ID)
            raise RolledBackError
        record_security_event(SecurityEvent.LOGOUT, user_id=USER_ID)

    assert [line["event"] for line in lines(caplog)] == ["email_added", "logout"]


class FailingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise RuntimeError("security sink unavailable")


@pytest.mark.django_db(transaction=True)
def test_a_change_line_that_cannot_be_written_at_the_commit_is_reported_and_skips_no_hook(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    reported_failures(caplog, monkeypatch)
    monkeypatch.setattr(logger, "handlers", [FailingHandler()])
    later: list[str] = []

    with transaction.atomic():
        record_security_event(SecurityEvent.LOGOUT, user_id=USER_ID)
        transaction.on_commit(lambda: later.append("ran"))

    assert later == ["ran"]
    assert [record.getMessage() for record in caplog.records] == [
        "security_line_failed event=logout error_type=RuntimeError"
    ]
