import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.contrib.auth.signals import user_login_failed
from django.db import connections
from django.http import HttpRequest
from django.test import Client, RequestFactory, override_settings

from pyracast.accounts import session_lock
from pyracast.accounts.audit import connect_security_events, logger, record_security_event
from pyracast.accounts.session_lock import serialize_session


@pytest.fixture(autouse=True)
def security_log(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    caplog.set_level(logging.INFO, logger="pyracast.security")


def test_authentication_failure_logs_once_without_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connect_security_events()
    connect_security_events()
    request = RequestFactory().post("/api/auth/browser/v1/auth/login")
    user_login_failed.send(
        sender=User,
        request=request,
        credentials={
            "email": "private@example.com",
            "password": "test-only-password",
            "token": "test-only-reset-token",
        },
    )
    assert len(caplog.records) == 1
    assert json.loads(caplog.records[0].message) == {
        "event": "login_failed",
        "user_id": None,
        "client_ip": "127.0.0.1",
    }
    assert "private@example.com" not in caplog.text
    assert "test-only" not in caplog.text


@pytest.mark.parametrize("http_request", [None, HttpRequest()])
def test_authentication_events_without_http_metadata_do_not_break_login(
    caplog: pytest.LogCaptureFixture, http_request: HttpRequest | None
) -> None:
    record_security_event("login_succeeded", request=http_request, user=User(pk=31))
    assert json.loads(caplog.records[0].message) == {
        "event": "login_succeeded",
        "user_id": 31,
        "client_ip": None,
    }


def test_email_verification_logs_identity_without_address_or_confirmation_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    record_security_event(
        "email_verified",
        email_address=EmailAddress(user_id=48, email="private@example.com"),
        confirmation="test-only-private-key",
    )
    assert json.loads(caplog.records[0].message) == {
        "event": "email_verified",
        "user_id": 48,
        "client_ip": None,
    }
    assert "private" not in caplog.text


def test_untrusted_address_cannot_inject_log_records(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from allauth.account.adapter import DefaultAccountAdapter

    monkeypatch.setattr(DefaultAccountAdapter, "get_client_ip", lambda self, request: "\nforged")
    record_security_event("login_failed", request=HttpRequest())
    assert json.loads(caplog.records[0].message)["client_ip"] is None
    assert "forged" not in caplog.text


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
@pytest.mark.parametrize("cause", ["session_busy", "rate_limit"])
@override_settings(ACCOUNT_RATE_LIMITS={"login": "1/m/ip"})
def test_authentication_429_logs_throttling_for_session_contention_and_rate_limits(
    cause: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/session/").json()["csrf_token"]

    def login() -> int:
        try:
            return client.post(
                "/api/auth/browser/v1/auth/login",
                {"email": "private@example.com", "password": "test-only-private-password"},
                content_type="application/json",
                HTTP_X_CSRFTOKEN=token,
            ).status_code
        finally:
            connections.close_all()

    if cause == "session_busy":
        monkeypatch.setattr(session_lock, "_LOCK_TIMEOUT", "100ms")
        key = client.session.session_key
        assert key is not None
        with serialize_session(key), ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(login).result(timeout=5) == 429
    else:
        assert login() == 400
        assert login() == 429

    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "pyracast.security"
    ]
    assert {
        "event": "authentication_request_throttled",
        "user_id": None,
        "client_ip": "127.0.0.1",
    } in events
    assert all(event["event"] != "authentication_rate_limited" for event in events)
    assert "private@example.com" not in caplog.text
    assert "test-only-private-password" not in caplog.text
