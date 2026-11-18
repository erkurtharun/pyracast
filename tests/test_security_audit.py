import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.contrib.auth.signals import user_login_failed
from django.core.exceptions import PermissionDenied
from django.db import connections
from django.http import HttpRequest
from django.test import Client, RequestFactory, override_settings

from pyracast.accounts import session_lock
from pyracast.accounts.audit import connect_security_events, logger
from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.accounts.session_lock import serialize_session
from pyracast.api import error_pages
from tests.support.admin_recovery_security import AUTH, authenticated_account

PASSWORD = "test-only-" + "harbor-lantern-58!"


@pytest.fixture(autouse=True)
def security_log(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    caplog.set_level(logging.INFO, logger="pyracast.security")


def security_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        line
        for record in caplog.records
        if record.name == "pyracast.security"
        and (line := json.loads(record.message))["event"] != "login_succeeded"
    ]


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
    line = json.loads(caplog.records[0].message)
    assert re.fullmatch(r"[0-9a-f]{16}", line.pop("subject"))
    assert line == {"event": "login_failed", "user_id": None, "client_ip": "127.0.0.1"}
    assert "private@example.com" not in caplog.text
    assert "test-only" not in caplog.text


@pytest.mark.parametrize("http_request", [None, HttpRequest()])
def test_authentication_events_without_http_metadata_do_not_break_login(
    caplog: pytest.LogCaptureFixture, http_request: HttpRequest | None
) -> None:
    user_login_failed.send(sender=User, request=http_request, credentials={})
    assert json.loads(caplog.records[0].message) == {
        "event": "login_failed",
        "user_id": None,
        "client_ip": None,
    }


def test_untrusted_address_cannot_inject_log_records(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from allauth.account.adapter import DefaultAccountAdapter

    monkeypatch.setattr(DefaultAccountAdapter, "get_client_ip", lambda self, request: "\nforged")
    user_login_failed.send(sender=User, request=HttpRequest(), credentials={})
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


def signed_in_viewer() -> tuple[Client, Tenant, User]:
    tenant = Tenant.objects.create(name="Refusal workspace")
    viewer = User.objects.create_user(
        username="refused-viewer", email="refused-viewer@example.com", password=PASSWORD
    )
    EmailAddress.objects.create(user=viewer, email=viewer.email, primary=True, verified=True)
    Membership.objects.create(tenant=tenant, user=viewer, role=WorkspaceRole.VIEWER)
    client = Client(enforce_csrf_checks=True)
    client.force_login(viewer)
    return client, tenant, viewer


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_a_signed_in_refusal_is_recorded_once_a_minute_per_view_and_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, tenant, viewer = signed_in_viewer()
    token = client.get("/api/session/").json()["csrf_token"]
    invitations = f"/api/tenants/{tenant.pk}/invitations/"
    for _ in range(3):
        refused = client.post(
            invitations,
            {"email": "guest@example.com"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        assert refused.status_code == 403
    assert client.get(f"/api/tenants/{tenant.pk}/members/").status_code == 403

    assert security_lines(caplog) == [
        {
            "event": "authorization_denied",
            "user_id": viewer.pk,
            "client_ip": "127.0.0.1",
            "tenant_id": tenant.pk,
            "reason": "workspace_admin_required",
            "view": "WorkspaceInvitationsView",
        },
        {
            "event": "authorization_denied",
            "user_id": viewer.pk,
            "client_ip": "127.0.0.1",
            "tenant_id": tenant.pk,
            "reason": "workspace_admin_required",
            "view": "WorkspaceMembersView",
        },
    ]
    assert "guest@example.com" not in caplog.text
    assert viewer.email not in caplog.text


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_a_signed_in_csrf_refusal_is_recorded_for_api_and_authentication_views(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, tenant, viewer = signed_in_viewer()
    api = client.post(f"/api/tenants/{tenant.pk}/sites/", {}, content_type="application/json")
    authentication = client.post(
        "/api/auth/browser/v1/auth/reauthenticate",
        {"password": PASSWORD},
        content_type="application/json",
    )

    assert api.status_code == authentication.status_code == 403
    assert api.json()["code"] == authentication.json()["code"] == "csrf_failed"
    assert [
        (line["event"], line["user_id"], line["reason"]) for line in security_lines(caplog)
    ] == [("csrf_refused", viewer.pk, "csrf_failed")] * 2


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_refusals_are_recorded_and_throttled_per_route_for_routes_without_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _, viewer = signed_in_viewer()
    for route in ("auth/reauthenticate", "account/password/change", "auth/reauthenticate"):
        refused = client.post(AUTH + route, {"password": PASSWORD}, content_type="application/json")
        assert refused.status_code == 403

    assert [(line["event"], line["user_id"], line["view"]) for line in security_lines(caplog)] == [
        ("csrf_refused", viewer.pk, "api/auth/browser/v1/auth/reauthenticate"),
        ("csrf_refused", viewer.pk, "api/auth/browser/v1/account/password/change"),
    ]


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_a_refusal_before_routing_is_recorded_with_its_route(
    caplog: pytest.LogCaptureFixture,
) -> None:
    operator = User.objects.create_user(username="route-operator", is_staff=True)
    client = Client()
    client.force_login(operator)

    assert client.get(AUTH + "account/authenticators/totp").status_code == 403

    assert [(line["event"], line["view"]) for line in security_lines(caplog)] == [
        ("authorization_denied", "api/auth/browser/v1/account/authenticators/totp")
    ]


def test_a_refusal_is_attributed_to_its_route_pattern_and_never_to_its_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = RequestFactory().get("/api/tenants/7341/regions/9257/forecast/")
    request.user = User(pk=990_001, username="route-pattern")

    assert error_pages.permission_denied(request, PermissionDenied()).status_code == 403

    [line] = security_lines(caplog)
    assert line["view"] == "api/tenants/<int:tenant_id>/regions/<int:region_id>/forecast/"
    assert "7341" not in caplog.text
    assert "9257" not in caplog.text


@pytest.mark.parametrize(
    ("refusal", "event", "code"),
    [
        ("permission", "authorization_denied", "permission_denied"),
        ("csrf", "csrf_refused", "csrf_failed"),
    ],
)
def test_a_refusal_without_a_matching_route_uses_a_fixed_route_label(
    refusal: str, event: str, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    request = RequestFactory().post("/api/unrouted/7341/private-value/")
    request.user = User(pk=990_002, username="unrouted-refusal")

    response = (
        error_pages.permission_denied(request, PermissionDenied())
        if refusal == "permission"
        else error_pages.csrf_failure(request)
    )

    assert response.status_code == 403
    assert json.loads(response.content)["code"] == code
    assert security_lines(caplog) == [
        {
            "event": event,
            "user_id": request.user.pk,
            "client_ip": "127.0.0.1",
            "reason": code,
            "view": "-",
        }
    ]
    assert "7341" not in caplog.text
    assert "private-value" not in caplog.text


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_a_signed_in_refusal_from_an_authentication_view_is_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, administrator, _ = authenticated_account()
    token = client.get("/api/session/").json()["csrf_token"]

    response = client.delete(AUTH + "account/authenticators/totp", HTTP_X_CSRFTOKEN=token)

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    assert [
        (line["event"], line["user_id"], line["reason"]) for line in security_lines(caplog)
    ] == [("authorization_denied", administrator.pk, "permission_denied")]
    assert administrator.email not in caplog.text


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_signed_out_refusals_write_no_security_line(caplog: pytest.LogCaptureFixture) -> None:
    client = Client(enforce_csrf_checks=True)
    assert client.get("/api/tenants/").status_code == 401
    assert (
        client.post(
            "/api/auth/browser/v1/auth/login",
            {"email": "someone@example.com", "password": PASSWORD},
            content_type="application/json",
        ).status_code
        == 403
    )
    assert security_lines(caplog) == []
