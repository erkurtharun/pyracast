import json
import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.conf import LazySettings
from django.contrib.auth.models import User
from django.contrib.sessions.exceptions import SessionInterrupted
from django.test import Client

from pyracast.accounts import headless, session_lock
from pyracast.accounts.audit import logger as security_logger

pytestmark = pytest.mark.django_db


def assert_private_json(response: Any, status: int, body: dict[str, object]) -> None:
    assert response.status_code == status
    assert response["Content-Type"] == "application/json"
    assert "no-store" in response["Cache-Control"]
    assert response.json() == body


def test_unknown_api_path_answers_json_404() -> None:
    assert_private_json(Client().get("/api/does-not-exist/"), 404, {"code": "not_found"})


def test_paths_outside_the_api_keep_html_errors() -> None:
    response = Client().get("/does-not-exist/")
    assert response.status_code == 404
    assert response["Content-Type"].startswith("text/html")


def test_invalid_trusted_proxy_headers_answer_json_403_without_caching(
    settings: LazySettings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.PYRACAST_TRUST_PROXY = True
    settings.PYRACAST_TRUSTED_PROXY_CIDRS = ["10.0.0.0/24"]
    monkeypatch.setattr(security_logger, "handlers", [caplog.handler])
    caplog.set_level(logging.INFO, logger="pyracast.security")
    response = Client(REMOTE_ADDR="10.0.0.7").get(
        "/api/session/", HTTP_X_FORWARDED_PROTO="https", HTTP_X_REAL_IP="not-an-address"
    )
    assert_private_json(response, 403, {"code": "permission_denied"})
    assert [json.loads(record.getMessage()) for record in caplog.records] == [
        {"client_ip": "10.0.0.7", "event": "invalid_proxy_headers", "user_id": None}
    ]


def test_interrupted_session_answers_session_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def interrupted(session_key: str) -> Iterator[None]:
        yield
        raise SessionInterrupted("The session serialization lock was lost.")

    monkeypatch.setattr(session_lock, "serialize_session", interrupted)
    client = Client()
    client.cookies["sessionid"] = "a" * 32
    assert_private_json(client.get("/api/session/"), 400, {"code": "session_ended"})


def test_privileged_totp_enrollment_refusal_is_json() -> None:
    staff = User.objects.create_user(username="operator", is_staff=True)
    client = Client()
    client.force_login(staff)
    response = client.get("/api/auth/browser/v1/account/authenticators/totp")
    assert_private_json(response, 403, {"code": "permission_denied"})


def test_unhandled_error_in_allauth_view_answers_json_500(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("private failure detail")

    monkeypatch.setattr(headless.LoginView, "post", fail)
    response = Client(raise_request_exception=False).post(
        "/api/auth/browser/v1/auth/login", {}, content_type="application/json"
    )
    assert response.status_code == 500
    assert "no-store" in response["Cache-Control"]
    body = response.json()
    assert body["code"] == "server_error"
    assert re.fullmatch(r"[0-9a-f]{32}", body["request_id"])
    assert "private failure detail" not in response.content.decode()
    [record] = [record for record in pyracast_logs.records if record.name == "pyracast.api"]
    assert record.exc_info is not None
    assert record.getMessage() == (
        f"api_server_error request_id={body['request_id']} method=POST "
        "path=/api/auth/browser/v1/auth/login view=-"
    )


def test_django_request_errors_reach_the_console_handler() -> None:
    request_logger = logging.getLogger("django.request")
    assert request_logger.level == logging.ERROR
    assert [
        handler.stream
        for handler in request_logger.handlers
        if isinstance(handler, logging.StreamHandler)
    ] == [sys.stdout]
    assert logging.getLogger("django.security").level == logging.WARNING
    assert logging.getLogger("django.security.DisallowedHost").disabled is False
    assert logging.getLogger("django.security.DisallowedHost").handlers == []
    assert not logging.getLogger("django.security.DisallowedHost").propagate
