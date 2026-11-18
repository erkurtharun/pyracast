import json
import logging
import re
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
from django.dispatch import Signal
from django.test import RequestFactory

from pyracast.accounts.audit import (
    LINE_KEYS,
    SIGNAL_EVENTS,
    RefusalThrottle,
    SecurityEvent,
    connect_security_events,
    logger,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "pyracast"
ADDRESS = "Private.Person@Example.com"
HIDDEN = "test-only-hidden-value"
USER_ID = 4821


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
            "from_email_address": None,
            "to_email_address": address(),
        },
        account_signals.password_changed: by_user,
        account_signals.password_set: by_user,
        account_signals.password_reset: by_user,
        account_signals.login_code_rejected: rejected_code,
        account_signals.password_reset_code_rejected: rejected_code,
        account_signals.email_verification_code_rejected: rejected_code,
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


def test_every_event_has_one_writer_and_every_writer_names_an_event() -> None:
    written = {
        name
        for path in SOURCE.rglob("*.py")
        if path.name != "audit.py"
        for name in re.findall(r"SecurityEvent\.([A-Z_]+)", path.read_text(encoding="utf-8"))
    }
    signalled = {event.name for event in SIGNAL_EVENTS.values()}

    assert written <= set(SecurityEvent.__members__)
    assert not written & signalled
    assert written | signalled == set(SecurityEvent.__members__)
    assert len(signalled) == len(SIGNAL_EVENTS)


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
