import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier, Event, Lock
from typing import Any

import pytest
from allauth.account.internal.flows.code_verification import AbstractCodeVerificationProcess
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import DatabaseError, InterfaceError, OperationalError, connections
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings

from pyracast.accounts import session_lock
from pyracast.accounts.mfa import MFAAdapter
from pyracast.accounts.session_lock import SessionRequestSerialization, serialize_session
from pyracast.api.session import SessionView
from tests.test_auth_lifecycle import AUTH, PASSWORD, mailed_code, member, submit, totp_code

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}


def pending_code(flow: str) -> tuple[Client, User, str, str]:
    client = Client(enforce_csrf_checks=True)
    if flow == "verify":
        assert (
            submit(
                client,
                "auth/signup",
                {"email": "owner@example.com", "password": PASSWORD, "company_name": "Owner"},
            ).status_code
            == 401
        )
        user = User.objects.get(email="owner@example.com")
        endpoint = "auth/email/verify"
    else:
        user = member()
        assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
        endpoint = "auth/password/reset"
    return client, user, mailed_code(), endpoint


def clone_client(origin: Client) -> Client:
    client = Client(enforce_csrf_checks=True)
    for name in (settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME):
        client.cookies[name] = origin.cookies[name].value
    return client


def post_code(client: Client, endpoint: str, code: str, suffix: str = "new") -> Any:
    payload = {"key": code}
    if endpoint == "auth/password/reset":
        payload["password"] = PASSWORD + suffix
    return client.post(
        AUTH + endpoint,
        payload,
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value,
    )


@pytest.mark.parametrize("flow", ["verify", "reset"])
@override_settings(MAILERS=MAILERS)
def test_parallel_wrong_codes_cannot_lose_attempts(
    flow: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, user, code, endpoint = pending_code(flow)
    entered, release, overlap = Event(), Event(), Event()
    ownership = Lock()
    original = AbstractCodeVerificationProcess.record_invalid_attempt

    def delayed_attempt(process: Any) -> bool:
        with ownership:
            first = not entered.is_set()
            (entered if first else overlap).set()
        if first:
            assert release.wait(timeout=10)
        return bool(original(process))

    monkeypatch.setattr(AbstractCodeVerificationProcess, "record_invalid_attempt", delayed_attempt)
    clients = [clone_client(origin) for _ in range(8)]
    barrier = Barrier(8)

    def attempt(client: Client) -> int:
        try:
            barrier.wait(timeout=10)
            return int(post_code(client, endpoint, "WRONG-CODE").status_code)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(attempt, client) for client in clients]
        try:
            assert entered.wait(timeout=10)
            assert not overlap.wait(timeout=0.5)
        finally:
            release.set()
        statuses = [future.result(timeout=10) for future in futures]
    assert statuses.count(400) == 3
    assert statuses.count(409) == 5
    assert post_code(origin, endpoint, code).status_code == 409
    user.refresh_from_db()
    assert user.check_password(PASSWORD)
    assert flow != "verify" or not EmailAddress.objects.get(user=user).verified


@pytest.mark.parametrize("flow", ["verify", "reset"])
@override_settings(MAILERS=MAILERS)
def test_parallel_correct_codes_are_consumed_once(flow: str) -> None:
    origin, user, code, endpoint = pending_code(flow)
    clients = [clone_client(origin) for _ in range(8)]
    barrier = Barrier(8)

    def attempt(index: int) -> int:
        try:
            barrier.wait(timeout=10)
            return int(post_code(clients[index], endpoint, code, suffix=f"new-{index}").status_code)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(attempt, range(8)))
    success = 200 if flow == "verify" else 401
    assert statuses.count(success) == 1
    assert statuses.count(409) == 7
    if flow == "reset":
        user.refresh_from_db()
        assert user.check_password(PASSWORD + f"new-{statuses.index(success)}")


@override_settings(MAILERS=MAILERS)
def test_ordinary_api_response_cannot_restore_stale_code_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    origin = Client(enforce_csrf_checks=True)
    assert (
        submit(origin, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200
    )
    assert submit(origin, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    assert submit(origin, "account/email", {"email": "new@example.com"}).status_code == 200
    session = origin.session
    session["last_activity_at"] = time.time() - 61
    session.save()
    entered, release, finished = Event(), Event(), Event()
    original = SessionView.get

    def delayed_get(view: SessionView, request: Any) -> Any:
        result = original(view, request)
        entered.set()
        assert release.wait(timeout=10)
        return result

    monkeypatch.setattr(SessionView, "get", delayed_get)

    def ordinary_request() -> int:
        try:
            return clone_client(origin).get("/api/session/").status_code
        finally:
            connections.close_all()

    def wrong_code() -> int:
        try:
            return int(
                post_code(clone_client(origin), "auth/email/verify", "WRONG-CODE").status_code
            )
        finally:
            finished.set()
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ordinary = pool.submit(ordinary_request)
        assert entered.wait(timeout=10)
        wrong = pool.submit(wrong_code)
        try:
            assert not finished.wait(timeout=0.5)
        finally:
            release.set()
        assert ordinary.result(timeout=10) == 200
        assert wrong.result(timeout=10) == 400
    assert origin.session["account_email_verification_code"]["failed_attempts"] == 1


@override_settings(MAILERS=MAILERS)
def test_provisioned_legacy_admin_needs_totp_after_email_code() -> None:
    user = User.objects.create_superuser(username="legacy-admin", password=PASSWORD)
    call_command(
        "provision_admin_mfa",
        username=user.username,
        email="operator@example.com",
        stdout=StringIO(),
    )
    client = Client(enforce_csrf_checks=True)
    response = submit(client, "auth/login", {"email": "operator@example.com", "password": PASSWORD})
    assert response.status_code == 401
    assert any(
        flow["id"] == "verify_email" and flow.get("is_pending")
        for flow in response.json()["data"]["flows"]
    )
    response = submit(client, "auth/email/verify", {"key": mailed_code()})
    assert response.status_code == 401
    assert not response.json()["meta"]["is_authenticated"]
    assert client.get("/admin/").status_code == 302
    assert client.get("/api/platform/accounts/").status_code == 403
    secret = MFAAdapter().decrypt(user.authenticator_set.get(type="totp").data["secret"])
    assert (
        submit(
            client, "auth/2fa/authenticate", {"code": totp_code(secret, time.time())}
        ).status_code
        == 200
    )
    assert client.get("/admin/").status_code == 200
    assert client.get("/api/platform/accounts/").status_code == 405


def acquire_on_another_connection(key: str) -> bool:
    try:
        with serialize_session(key):
            return True
    finally:
        connections.close_all()


@pytest.mark.parametrize(
    "phase",
    [
        "acquire",
        "restore",
        "commit",
        "release",
        "restore_non_database",
        "release_interface",
        "release_non_database",
    ],
)
def test_database_failure_does_not_leak_a_session_lock(
    phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_lock, "_LOCK_TIMEOUT", "100ms")
    connection = connections["default"]
    key = "a" * 32

    def fail(execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any) -> Any:
        target = (
            (phase == "acquire" and "current_setting" in sql)
            or (
                phase in {"restore", "restore_non_database"}
                and "set_config" in sql
                and params[0] != "100ms"
            )
            or (
                phase in {"release", "release_interface", "release_non_database"}
                and "pg_advisory_unlock" in sql
            )
        )
        if target:
            if phase in {"restore_non_database", "release_non_database"}:
                raise RuntimeError("Injected non-database failure")
            if phase == "release_interface":
                raise InterfaceError("Injected interface failure")
            raise OperationalError("Injected database failure")
        return execute(sql, params, many, context)

    def fail_commit() -> None:
        raise OperationalError("Injected commit failure")

    if phase == "commit":
        monkeypatch.setattr(connection, "commit", fail_commit)
    with (
        connection.execute_wrapper(fail),
        pytest.raises((DatabaseError, InterfaceError, RuntimeError), match="Injected"),
        serialize_session(key),
    ):
        pass
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(acquire_on_another_connection, key).result(timeout=5)
        assert connection.connection is None
    finally:
        connection.close()


@pytest.mark.parametrize("loss", ["wrapper", "reconnected", "transport"])
def test_connection_loss_fails_closed_and_releases_lock(loss: str) -> None:
    connection = connections["default"]
    key = "b" * 32

    def lose_connection() -> None:
        with serialize_session(key):
            if loss == "transport":
                assert connection.connection is not None
                connection.connection.close()
            else:
                connection.close()
            if loss == "reconnected":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")

    with pytest.raises(SessionInterrupted, match="serialization connection"):
        lose_connection()
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(acquire_on_another_connection, key).result(timeout=5)


def test_handler_failure_releases_the_session_lock() -> None:
    key = "f" * 32
    incoming = RequestFactory().get("/")
    incoming.COOKIES[settings.SESSION_COOKIE_NAME] = key

    def fail(_: Any) -> HttpResponse:
        raise RuntimeError("Handler failed")

    with pytest.raises(RuntimeError, match="Handler failed"):
        SessionRequestSerialization(fail)(incoming)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(acquire_on_another_connection, key).result(timeout=5)


def test_missing_lock_fails_closed() -> None:
    connection = connections["default"]
    key = "c" * 32
    with (
        pytest.raises(SessionInterrupted, match="serialization lock"),
        serialize_session(key),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT pg_advisory_unlock_all()")
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(acquire_on_another_connection, key).result(timeout=5)


def test_contention_has_a_bounded_wait_and_retry_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_lock, "_LOCK_TIMEOUT", "100ms")
    key = "d" * 32

    def request() -> HttpResponse:
        try:
            incoming = RequestFactory().get("/")
            incoming.COOKIES[settings.SESSION_COOKIE_NAME] = key
            return SessionRequestSerialization(lambda _: HttpResponse("ok"))(incoming)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with serialize_session(key):
            response = pool.submit(request).result(timeout=5)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "1"
        assert pool.submit(request).result(timeout=5).status_code == 200


@pytest.mark.parametrize("key", ["", "a" * 31, "!" * 32, "?" * 32, "A" * 32])
def test_malformed_cookies_do_not_acquire_a_lock(key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_lock(_: str) -> None:
        raise AssertionError("Malformed cookie reached session serialization.")

    monkeypatch.setattr(session_lock, "serialize_session", unexpected_lock)
    incoming = RequestFactory().get("/")
    incoming.COOKIES[settings.SESSION_COOKIE_NAME] = key
    assert SessionRequestSerialization(lambda _: HttpResponse("ok"))(incoming).status_code == 200


def test_connection_pool_configuration_is_rejected() -> None:
    connection = connections["default"]
    previous = connection.settings_dict["OPTIONS"]
    connection.settings_dict["OPTIONS"] = {"pool": True}
    try:
        with (
            pytest.raises(ImproperlyConfigured, match="direct PostgreSQL"),
            serialize_session("e" * 32),
        ):
            pass
    finally:
        connection.settings_dict["OPTIONS"] = previous


@pytest.mark.parametrize("state", ["missing", "expired"])
@override_settings(MAILERS=MAILERS)
def test_missing_or_expired_session_cannot_restore_a_code(state: str) -> None:
    client, user, code, endpoint = pending_code("reset")
    key = client.cookies[settings.SESSION_COOKIE_NAME].value
    if state == "missing":
        Session.objects.filter(session_key=key).delete()
    else:
        from datetime import timedelta

        from django.utils import timezone

        Session.objects.filter(session_key=key).update(
            expire_date=timezone.now() - timedelta(seconds=1)
        )
    assert post_code(client, endpoint, code).status_code == 409
    assert client.get("/api/session/").json()["user"] is None
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


@pytest.mark.parametrize("mode", ["header", "query", "plain"])
@override_settings(MAILERS=MAILERS)
def test_get_cannot_consume_email_verification_attempts(mode: str) -> None:
    client, user, code, _ = pending_code("verify")
    for _ in range(3):
        if mode == "header":
            response = client.get(AUTH + "auth/email/verify", HTTP_X_EMAIL_VERIFICATION_KEY=code)
        elif mode == "query":
            response = client.get(AUTH + "auth/email/verify", {"key": code})
        else:
            response = client.get(AUTH + "auth/email/verify")
        assert response.status_code == 405
    assert client.session["account_email_verification_code"]["failed_attempts"] == 0
    assert not EmailAddress.objects.get(user=user).verified
    assert submit(client, "auth/email/verify", {"key": code}).status_code == 200


@pytest.mark.parametrize("flow", ["verify", "reset"])
@pytest.mark.parametrize("method", ["head", "options", "put", "patch", "delete"])
@override_settings(MAILERS=MAILERS)
def test_safe_methods_cannot_consume_code_attempts(flow: str, method: str) -> None:
    client, user, code, endpoint = pending_code(flow)
    response = client.generic(
        method.upper(),
        AUTH + endpoint,
        data=b"{invalid-json",
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value,
    )
    assert response.status_code == 405
    state_key = (
        "account_email_verification_code"
        if flow == "verify"
        else "account_password_reset_verification"
    )
    assert client.session[state_key]["failed_attempts"] == 0
    expected = 200 if flow == "verify" else 401
    assert post_code(client, endpoint, code).status_code == expected
    user.refresh_from_db()
    assert user.check_password(PASSWORD if flow == "verify" else PASSWORD + "new")


@pytest.mark.parametrize("flow", ["verify", "reset"])
@override_settings(MAILERS=MAILERS)
def test_code_post_requires_csrf_and_preserves_pending_code_on_denial(flow: str) -> None:
    client, _, code, endpoint = pending_code(flow)
    payload = {"key": code}
    if flow == "reset":
        payload["password"] = PASSWORD + "new"
    assert client.post(AUTH + endpoint, payload, content_type="application/json").status_code == 403
    state_key = (
        "account_email_verification_code"
        if flow == "verify"
        else "account_password_reset_verification"
    )
    assert client.session[state_key]["failed_attempts"] == 0
    assert post_code(client, endpoint, code).status_code == (200 if flow == "verify" else 401)


@override_settings(MAILERS=MAILERS)
def test_verification_resend_is_throttled_capped_and_invalidates_older_codes() -> None:
    client, _, initial, _ = pending_code("verify")
    assert submit(client, "auth/email/verify/resend", {}).status_code == 429
    assert client.session["account_email_verification_code"]["code"] == initial
    latest = initial
    for _ in range(3):
        cache.clear()
        assert submit(client, "auth/email/verify/resend", {}).status_code == 200
        latest = mailed_code()
    assert submit(client, "auth/email/verify/resend", {}).status_code == 409
    assert submit(client, "auth/email/verify", {"key": initial}).status_code == 400
    assert submit(client, "auth/email/verify", {"key": latest}).status_code == 200
