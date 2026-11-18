import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from types import SimpleNamespace

import pytest
from allauth.account.internal.flows.login import get_authentication_records, record_authentication
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal import auth as totp_auth
from allauth.mfa.totp.internal.auth import format_hotp_value, hotp_value
from django.contrib.auth.models import AnonymousUser, User
from django.db import connections
from django.http import HttpRequest
from django.test import Client, RequestFactory, override_settings

from pyracast.accounts import mfa_views
from pyracast.accounts.mfa_views import recently_reauthenticated_with_mfa
from tests.support.admin_mfa_bootstrap import TEST_PASSWORD
from tests.support.admin_recovery_security import AUTH, RECOVERY, authenticated_account, submit

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("privileged", [False, True])
def test_session_and_password_cannot_read_or_replace_recovery_codes(
    method: str,
    privileged: bool,
) -> None:
    client, user, _ = authenticated_account(privileged=privileged)
    recovery = RecoveryCodes.activate(user).instance
    original = recovery.data.copy()
    assert (
        submit(client, AUTH + "auth/reauthenticate", {"password": TEST_PASSWORD}).status_code == 403
    )
    response = client.get(RECOVERY) if method == "get" else submit(client, RECOVERY, {})
    assert response.status_code == 401
    assert response.json()["errors"][0]["code"] == "mfa_reauthentication_required"
    assert response.json()["meta"]["is_authenticated"]
    assert "no-store" in response.headers["Cache-Control"]
    recovery.refresh_from_db()
    assert recovery.data == original
    assert Authenticator.objects.filter(user=user, type="recovery_codes").count() == 1


@pytest.mark.parametrize("factor", ["totp", "recovery_codes"])
@pytest.mark.parametrize("privileged", [False, True])
@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_fresh_mfa_reauthentication_allows_recovery_code_rotation(
    factor: str, privileged: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, user, secret = authenticated_account(privileged=privileged)
    recovery = RecoveryCodes.activate(user)
    original = recovery.generate_codes()
    now = time.time()
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now))
    code = (
        format_hotp_value(hotp_value(secret, int(now) // 30)) if factor == "totp" else original[0]
    )
    assert submit(client, AUTH + "auth/2fa/reauthenticate", {"code": code}).status_code == 200
    response = submit(client, RECOVERY, {})
    assert response.status_code == 200
    assert response.json()["data"]["unused_codes"] != original
    viewed = client.get(RECOVERY)
    if factor == "totp":
        assert "unused_codes" not in viewed.json()["data"]
    else:
        assert viewed.status_code == 401

    monkeypatch.setattr(time, "time", lambda: now + 301)
    assert (
        submit(client, AUTH + "auth/reauthenticate", {"password": TEST_PASSWORD}).status_code == 403
    )
    assert submit(client, RECOVERY, {}).status_code == 401


@pytest.mark.parametrize("invalid_at", [None, "invalid", float("nan"), float("inf"), -1])
def test_invalid_mfa_reauthentication_timestamp_cannot_authorize_recovery_codes(
    invalid_at: object,
) -> None:
    client, user, _ = authenticated_account()
    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    record = get_authentication_records(request, user)[0]
    record["reauthenticated"] = True
    record["at"] = invalid_at
    assert not recently_reauthenticated_with_mfa(request)


def test_deleted_mfa_factor_cannot_authorize_recovery_codes() -> None:
    client, user, _ = authenticated_account()
    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    get_authentication_records(request, user)[0]["reauthenticated"] = True
    Authenticator.objects.filter(user=user).delete()
    assert not recently_reauthenticated_with_mfa(request)


def test_mfa_reauthentication_is_bound_to_current_account() -> None:
    client, user, _ = authenticated_account(privileged=False)
    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    get_authentication_records(request, user)[0]["reauthenticated"] = True
    assert recently_reauthenticated_with_mfa(request)
    request.user = User.objects.create_user(username="other")
    Authenticator.objects.create(user=request.user, type="totp", data={})
    assert not recently_reauthenticated_with_mfa(request)


def test_anonymous_request_has_no_mfa_reauthentication() -> None:
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    assert not recently_reauthenticated_with_mfa(request)


def test_password_reauthentication_cannot_remove_customer_totp() -> None:
    client, user, _ = authenticated_account(privileged=False)
    assert (
        submit(client, AUTH + "auth/reauthenticate", {"password": TEST_PASSWORD}).status_code == 403
    )
    token = client.get("/api/session/").json()["csrf_token"]
    response = client.delete(AUTH + "account/authenticators/totp", HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 401
    assert response.json()["errors"][0]["code"] == "mfa_reauthentication_required"
    assert Authenticator.objects.filter(user=user, type="totp").exists()


@pytest.mark.parametrize("privileged", [False, True])
@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_mfa_reauthentication_allows_only_customers_to_remove_totp(
    privileged: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, user, secret = authenticated_account(privileged=privileged)
    other = User.objects.create_user(username="other")
    other_factor = Authenticator.objects.create(user=other, type="totp", data={})
    now = time.time()
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now))
    code = format_hotp_value(hotp_value(secret, int(now) // 30))
    assert submit(client, AUTH + "auth/2fa/reauthenticate", {"code": code}).status_code == 200
    assert client.delete(AUTH + "account/authenticators/totp").status_code == 403
    token = client.get("/api/session/").json()["csrf_token"]
    response = client.delete(AUTH + "account/authenticators/totp", HTTP_X_CSRFTOKEN=token)
    assert response.status_code == (403 if privileged else 200)
    assert Authenticator.objects.filter(user=user, type="totp").exists() is privileged
    assert Authenticator.objects.filter(pk=other_factor.pk, user=other).exists()
    assert "no-store" in response.headers["Cache-Control"]


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", RECOVERY), ("post", RECOVERY), ("delete", AUTH + "account/authenticators/totp")],
)
def test_anonymous_mfa_management_reports_unauthenticated(method: str, path: str) -> None:
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/session/").json()["csrf_token"]
    response = getattr(client, method)(path, HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 401
    assert response.json()["meta"]["is_authenticated"] is False


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", RECOVERY), ("post", RECOVERY), ("delete", AUTH + "account/authenticators/totp")],
)
def test_missing_factor_does_not_bypass_mfa_management_guard(method: str, path: str) -> None:
    client, user, _ = authenticated_account(privileged=False)
    Authenticator.objects.filter(user=user).delete()
    assert (
        submit(client, AUTH + "auth/reauthenticate", {"password": TEST_PASSWORD}).status_code == 200
    )
    token = client.get("/api/session/").json()["csrf_token"]
    response = getattr(client, method)(path, HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 401
    assert response.json()["errors"][0]["code"] == "mfa_reauthentication_required"


def test_recovery_head_requires_explicit_mfa_reauthentication() -> None:
    client, user, _ = authenticated_account(privileged=False)
    recovery = RecoveryCodes.activate(user).instance
    original = recovery.data.copy()
    assert client.head(RECOVERY).status_code == 401
    recovery.refresh_from_db()
    assert recovery.data == original


def test_recovery_codes_cannot_be_deleted_through_unsupported_http_method() -> None:
    client, user, _ = authenticated_account(privileged=False)
    recovery = RecoveryCodes.activate(user).instance
    original = recovery.data.copy()
    token = client.get("/api/session/").json()["csrf_token"]
    response = client.delete(RECOVERY, HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 405
    assert set(response.headers["Allow"].split(", ")) == {"GET", "POST", "HEAD", "OPTIONS"}
    assert "no-store" in response.headers["Cache-Control"]
    recovery.refresh_from_db()
    assert recovery.data == original


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_recovery_access_and_factor_removal_serialize_across_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_client, user, _ = authenticated_account(privileged=False)
    factor = Authenticator.objects.get(user=user, type="totp")
    recovery_codes = RecoveryCodes.activate(user).generate_codes()
    removal_client = Client(enforce_csrf_checks=True)
    removal_client.force_login(user)
    for client in (recovery_client, removal_client):
        request = RequestFactory().get("/")
        request.user = user
        request.session = client.session
        record_authentication(request, user, method="mfa", type="totp", id=factor.pk)
        get_authentication_records(request, user)[-1]["reauthenticated"] = True
        request.session.save()
    token = removal_client.get("/api/session/").json()["csrf_token"]
    verified, release, removal_finished = Event(), Event(), Event()
    recovery_pid: Queue[int] = Queue()
    removal_pid: Queue[int] = Queue()
    original = mfa_views.recently_reauthenticated_with_mfa

    def pause_after_verification(request: HttpRequest) -> bool:
        authenticated = original(request)
        if request.path == RECOVERY:
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                recovery_pid.put(int(cursor.fetchone()[0]))
            verified.set()
            assert release.wait(timeout=10)
        return authenticated

    monkeypatch.setattr(mfa_views, "recently_reauthenticated_with_mfa", pause_after_verification)

    def access_recovery() -> tuple[int, list[str]]:
        try:
            response = recovery_client.get(RECOVERY)
            return response.status_code, response.json().get("data", {}).get("unused_codes", [])
        finally:
            connections.close_all()

    def remove_factor() -> int:
        try:
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                removal_pid.put(int(cursor.fetchone()[0]))
            return removal_client.delete(
                AUTH + "account/authenticators/totp", HTTP_X_CSRFTOKEN=token
            ).status_code
        finally:
            removal_finished.set()
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        recovery_future = pool.submit(access_recovery)
        try:
            assert verified.wait(timeout=10)
            holder_pid = recovery_pid.get(timeout=1)
            removal_future = pool.submit(remove_factor)
            waiter_pid = removal_pid.get(timeout=5)
            blocked = False
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with connections["default"].cursor() as cursor:
                    cursor.execute(
                        "SELECT %s = ANY(pg_blocking_pids(%s))", [holder_pid, waiter_pid]
                    )
                    blocked = bool(cursor.fetchone()[0])
                if blocked or removal_finished.wait(timeout=0.01):
                    break
            assert blocked
        finally:
            release.set()
        assert recovery_future.result(timeout=10) == (200, recovery_codes)
        assert removal_future.result(timeout=10) == 200
    assert not Authenticator.objects.filter(user=user, type="totp").exists()
