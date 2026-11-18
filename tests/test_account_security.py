import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from runpy import run_path
from threading import Barrier

import pytest
from allauth.account.internal.flows.login import record_authentication
from allauth.account.models import EmailAddress
from allauth.core.internal.ratelimit import consume, get_cache_key, parse_rate
from allauth.mfa.models import Authenticator
from django.conf import LazySettings
from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.core import mail
from django.core.cache import cache
from django.core.checks import Tags, run_checks
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import close_old_connections, connections, transaction
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory, override_settings

from pyracast import settings as project_settings
from pyracast.accounts.cache import AuthenticationCache
from pyracast.accounts.checks import (
    check_authentication_mail_delivery,
    check_authentication_origin,
)
from pyracast.accounts.middleware import TrustedProxyHeaders
from pyracast.accounts.security import has_verified_mfa, normalize_client_ip
from pyracast.api.middleware import PrivateAPIResponses
from pyracast.mail.smtp import EmailBackend as SMTPEmailBackend


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
def test_authentication_cache_shares_values_and_honors_expiry() -> None:
    first = AuthenticationCache("", {})
    second = AuthenticationCache("", {})
    assert first.add("login", [1, 2])
    assert second.get("login") == [1, 2]
    assert not second.add("login", [3])
    first.set("login", [3], timeout=None)
    assert second.get("login") == [3]
    assert not second.add("login", [4])
    first.set("login", [5], timeout=0)
    assert second.get("login", "expired") == "expired"
    assert second.add("login", [6])
    assert first.get("login") == [6]
    assert first.delete("login")
    assert not second.delete("login")
    first.set("cleanup", True)
    second.clear()
    assert first.get("cleanup") is None


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
def test_authentication_cache_expiry_uses_database_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: 1.0)
    assert cache.add("host-clock-skew", True, timeout=60)
    assert cache.get("host-clock-skew") is True
    assert not cache.add("host-clock-skew", True, timeout=60)


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
def test_authentication_cache_survives_application_transaction_rollback() -> None:
    with transaction.atomic():
        assert cache.add("used-authenticator-code", True, timeout=60)
        transaction.set_rollback(True)
    assert cache.get("used-authenticator-code") is True
    assert not cache.add("used-authenticator-code", True, timeout=60)


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
def test_authentication_cache_refuses_transactional_connection() -> None:
    with (
        transaction.atomic(using="auth_cache"),
        pytest.raises(ImproperlyConfigured, match="autocommit"),
    ):
        cache.add("must-not-write", True)


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
@pytest.mark.parametrize("expired", [False, True])
def test_authentication_cache_add_has_one_winner_across_connections(expired: bool) -> None:
    if expired:
        cache.set("concurrent-lock", True, timeout=0)
    barrier = Barrier(8)

    def acquire() -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return bool(cache.add("concurrent-lock", True))
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: acquire(), range(8)))
    assert results.count(True) == 1


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
def test_allauth_rate_limit_cannot_be_exceeded_by_concurrent_writers() -> None:
    barrier = Barrier(8)

    def attempt() -> bool:
        close_old_connections()
        try:
            request = RequestFactory().post("/api/auth/browser/v1/auth/login")
            barrier.wait(timeout=10)
            return bool(
                consume(
                    request,
                    app="allauth.account",
                    action="login_failed",
                    key="member@example.com",
                    config={"login_failed": "3/m/key"},
                )
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: attempt(), range(8)))
    assert results.count(True) == 3


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
def test_authentication_cache_purges_at_most_one_hundred_expired_rows_per_write() -> None:
    with connections["auth_cache"].cursor() as cursor:
        cursor.execute(
            "INSERT INTO pyracast_authentication_cache (cache_key, value, expires) "
            "SELECT 'expired-' || n, 'true'::jsonb, clock_timestamp() - interval '1 second' "
            "FROM generate_series(1, 150) AS n"
        )
        cursor.execute(
            "INSERT INTO pyracast_authentication_cache (cache_key, value, expires) "
            "VALUES ('permanent', 'true'::jsonb, NULL)"
        )

        def remaining() -> tuple[int, int]:
            cursor.execute(
                "SELECT count(*) FILTER (WHERE cache_key LIKE 'expired-%%'), "
                "count(*) FILTER (WHERE expires IS NULL) FROM pyracast_authentication_cache"
            )
            row = cursor.fetchone()
            return row[0], row[1]

        cache.set("first-write", True)
        assert remaining() == (50, 1)
        cache.set("second-write", True)
        assert remaining() == (0, 1)


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
def test_a_held_rate_limit_lock_refuses_after_about_one_second() -> None:
    request = RequestFactory().post("/api/auth/browser/v1/auth/login")
    config = {"login_failed": "3/m/key"}
    assert consume(request, app="allauth.account", action="login_failed", key="held", config=config)
    rate = parse_rate(config["login_failed"])
    lock = get_cache_key(
        request, app="allauth.account", action="login_failed", rate=rate, key="held"
    )
    assert cache.add(f"{lock}:lock", True, timeout=10)
    started = time.monotonic()
    assert (
        consume(request, app="allauth.account", action="login_failed", key="held", config=config)
        is None
    )
    assert 0.9 <= time.monotonic() - started < 3


def proxy_response(request: HttpRequest) -> HttpResponse:
    return HttpResponse()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("remote", ["192.0.2.2", "173.245.48.1"])
def test_untrusted_client_cannot_supply_proxy_identity(
    settings: LazySettings, enabled: bool, remote: str
) -> None:
    settings.PYRACAST_TRUST_PROXY = enabled
    settings.PYRACAST_TRUSTED_PROXY_CIDRS = ["10.0.0.0/24"]
    settings.PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS = ["173.245.48.0/20"]
    request = RequestFactory().get(
        "/",
        REMOTE_ADDR=remote,
        HTTP_FORWARDED="for=127.0.0.1;proto=https",
        HTTP_X_FORWARDED_FOR="127.0.0.1",
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_FORWARDED_HOST="attacker.example",
        HTTP_X_REAL_IP="127.0.0.1",
        HTTP_CF_CONNECTING_IP="127.0.0.1",
    )
    TrustedProxyHeaders(proxy_response)(request)
    assert "HTTP_FORWARDED" not in request.META
    assert not any(name.startswith("HTTP_X_FORWARDED_") for name in request.META)
    assert "HTTP_X_REAL_IP" not in request.META
    assert "HTTP_CF_CONNECTING_IP" not in request.META


def test_proxy_trust_requires_explicit_networks(settings: LazySettings) -> None:
    settings.PYRACAST_TRUST_PROXY = True
    settings.PYRACAST_TRUSTED_PROXY_CIDRS = []
    with pytest.raises(ImproperlyConfigured, match="explicit"):
        TrustedProxyHeaders(proxy_response)


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://example.com/path",
        "https://x@example.com",
        "https:///",
        "https://[invalid",
        "https://example.com:invalid",
    ],
)
def test_production_rejects_unsafe_public_origins(settings: LazySettings, origin: str) -> None:
    settings.DEBUG = False
    settings.PYRACAST_PUBLIC_ORIGIN = origin
    with pytest.raises(ImproperlyConfigured, match="public origin"):
        TrustedProxyHeaders(proxy_response)
    assert [error.id for error in check_authentication_origin()] == ["accounts.E002"]


def test_development_accepts_local_http_origin(settings: LazySettings) -> None:
    settings.DEBUG = True
    settings.PYRACAST_PUBLIC_ORIGIN = "http://127.0.0.1:3000"
    TrustedProxyHeaders(proxy_response)
    assert check_authentication_origin() == []


def test_deployment_requires_production_mail_configuration(settings: LazySettings) -> None:
    settings.DEBUG = False
    settings.DEFAULT_FROM_EMAIL = "Pyracast <noreply@localhost>"
    assert [error.id for error in check_authentication_mail_delivery()] == ["accounts.E001"]
    settings.DEFAULT_FROM_EMAIL = "Pyracast <accounts@example.com>"
    settings.MAILERS = {
        "default": {
            "BACKEND": "pyracast.mail.smtp.EmailBackend",
            "OPTIONS": {"host": "smtp.example.com", "use_tls": True},
        }
    }
    assert check_authentication_mail_delivery() == []
    settings.DEBUG = True
    settings.DEFAULT_FROM_EMAIL = "noreply@localhost"
    assert check_authentication_mail_delivery() == []


@pytest.mark.parametrize("configured_backend", [None, "smtp"])
def test_configured_smtp_passes_registered_production_mail_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_backend: str | None
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYRACAST_DEBUG", "0")
    monkeypatch.setenv("PYRACAST_FROM_EMAIL", "Pyracast <accounts@example.com>")
    monkeypatch.setenv("PYRACAST_PUBLIC_ORIGIN", "https://app.example.com")
    monkeypatch.setenv("PYRACAST_EMAIL_HOST", "smtp.example.com")
    if configured_backend is None:
        monkeypatch.delenv("PYRACAST_EMAIL_BACKEND", raising=False)
    else:
        monkeypatch.setenv("PYRACAST_EMAIL_BACKEND", configured_backend)
    configuration = run_path(str(Path(project_settings.__file__)))
    with override_settings(
        DEBUG=configuration["DEBUG"],
        MAILERS=configuration["MAILERS"],
        DEFAULT_FROM_EMAIL=configuration["DEFAULT_FROM_EMAIL"],
        PYRACAST_PUBLIC_ORIGIN=configuration["PYRACAST_PUBLIC_ORIGIN"],
    ):
        assert isinstance(mail.mailers["default"], SMTPEmailBackend)
        checks = run_checks(tags=[Tags.security], include_deployment_checks=True)
    assert [
        check for check in checks if check.id is not None and check.id.startswith("accounts.")
    ] == []


@pytest.mark.parametrize(
    ("backend", "options"),
    [
        ("pyracast.mail.smtp.EmailBackend", {}),
        ("pyracast.mail.smtp.EmailBackend", {"host": ""}),
        ("django.core.mail.backends.smtp.EmailBackend", {"host": "smtp.example.com"}),
        ("django.core.mail.backends.console.EmailBackend", {"host": "smtp.example.com"}),
        ("django.core.mail.backends.locmem.EmailBackend", {"host": "smtp.example.com"}),
        ("custom.smtp.EmailBackend", {"host": "smtp.example.com"}),
    ],
)
def test_production_mail_check_rejects_unsupported_or_unconfigured_smtp(
    settings: LazySettings, backend: str, options: dict[str, str]
) -> None:
    settings.DEBUG = False
    settings.DEFAULT_FROM_EMAIL = "Pyracast <accounts@example.com>"
    settings.MAILERS = {"default": {"BACKEND": backend, "OPTIONS": options}}
    assert [error.id for error in check_authentication_mail_delivery()] == ["accounts.E001"]


@pytest.mark.parametrize(
    ("proto", "client_ip"),
    [("https,http", "192.0.2.1"), ("https", ""), ("https", "192.0.2.1, 10.0.0.1")],
)
def test_trusted_proxy_rejects_ambiguous_identity(
    settings: LazySettings, proto: str, client_ip: str
) -> None:
    settings.PYRACAST_TRUST_PROXY = True
    settings.PYRACAST_TRUSTED_PROXY_CIDRS = ["10.0.0.0/24"]
    request = RequestFactory().get(
        "/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_PROTO=proto, HTTP_X_REAL_IP=client_ip
    )
    with pytest.raises(PermissionDenied, match="Invalid trusted proxy"):
        TrustedProxyHeaders(proxy_response)(request)


def test_trusted_proxy_preserves_validated_scheme_and_client_ip(settings: LazySettings) -> None:
    settings.PYRACAST_TRUST_PROXY = True
    settings.PYRACAST_TRUSTED_PROXY_CIDRS = ["10.0.0.0/24"]
    settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    request = RequestFactory().get(
        "/",
        REMOTE_ADDR="10.0.0.1",
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_REAL_IP="2001:db8::1",
        HTTP_X_FORWARDED_FOR="198.51.100.1",
    )
    TrustedProxyHeaders(proxy_response)(request)
    assert request.is_secure()
    assert request.META["HTTP_X_REAL_IP"] == "2001:db8::1"
    assert "HTTP_X_FORWARDED_FOR" not in request.META


@pytest.mark.parametrize(
    ("chain", "expected"),
    [
        ("198.51.100.7, 173.245.48.1", "198.51.100.7"),
        ("127.0.0.1, 198.51.100.7, 173.245.48.1", "198.51.100.7"),
        ("invalid, 2001:db8::7, 173.245.48.1", "2001:db8::7"),
        ("127.0.0.1, ::ffff:198.51.100.7, 173.245.48.1", "198.51.100.7"),
        ("127.0.0.1, 198.51.100.7, 2400:cb00::1", "198.51.100.7"),
        ("127.0.0.1, 173.245.49.1, 173.245.48.1", "173.245.49.1"),
        ("198.51.100.7", "198.51.100.7"),
    ],
)
def test_proxy_chain_uses_gateway_peer_or_cloudflare_client(
    settings: LazySettings, chain: str, expected: str
) -> None:
    settings.PYRACAST_TRUST_PROXY = True
    settings.PYRACAST_TRUSTED_PROXY_CIDRS = ["172.30.80.2/32"]
    settings.PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS = ["173.245.48.0/20", "2400:cb00::/32"]
    request = RequestFactory().get(
        "/",
        REMOTE_ADDR="172.30.80.2",
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_REAL_IP="173.245.48.1",
        HTTP_X_FORWARDED_FOR=chain,
        HTTP_CF_CONNECTING_IP="127.0.0.1",
    )
    TrustedProxyHeaders(proxy_response)(request)
    assert request.META["HTTP_X_REAL_IP"] == expected
    assert "HTTP_X_FORWARDED_FOR" not in request.META
    assert "HTTP_CF_CONNECTING_IP" not in request.META


@pytest.mark.parametrize("chain", ["", "invalid", "198.51.100.7, invalid", "173.245.48.1"])
def test_proxy_chain_rejects_missing_or_invalid_client(settings: LazySettings, chain: str) -> None:
    settings.PYRACAST_TRUST_PROXY = True
    settings.PYRACAST_TRUSTED_PROXY_CIDRS = ["172.30.80.2/32"]
    settings.PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS = ["173.245.48.0/20"]
    request = RequestFactory().get(
        "/",
        REMOTE_ADDR="172.30.80.2",
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_REAL_IP="173.245.48.1",
        HTTP_X_FORWARDED_FOR=chain,
    )
    with pytest.raises(PermissionDenied, match="Invalid trusted proxy"):
        TrustedProxyHeaders(proxy_response)(request)


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
def test_cloudflare_visitors_keep_separate_rate_limits_and_cannot_forge_identity(
    settings: LazySettings,
) -> None:
    settings.PYRACAST_TRUST_PROXY = True
    settings.PYRACAST_TRUSTED_PROXY_CIDRS = ["172.30.80.2/32"]
    settings.PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS = ["173.245.48.0/20"]
    settings.ALLAUTH_TRUSTED_CLIENT_IP_HEADER = "X-Real-IP"
    allowed = []
    for chain in (
        "127.0.0.1, 198.51.100.7, 173.245.48.1",
        "192.0.2.1, 198.51.100.7, 173.245.48.1",
        "127.0.0.1, 198.51.100.8, 173.245.48.1",
    ):
        request = RequestFactory().post(
            "/",
            REMOTE_ADDR="172.30.80.2",
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_X_REAL_IP="173.245.48.1",
            HTTP_X_FORWARDED_FOR=chain,
        )
        TrustedProxyHeaders(proxy_response)(request)
        allowed.append(
            bool(
                consume(request, app="allauth.account", action="login", config={"login": "1/m/ip"})
            )
        )
    assert allowed == [True, False, True]


@pytest.mark.parametrize("address", ["", "invalid", "192.0.2.1, 192.0.2.2"])
def test_invalid_remote_address_is_denied_without_trusting_headers(address: str) -> None:
    request = RequestFactory().get("/", REMOTE_ADDR=address)
    with pytest.raises(PermissionDenied, match="Invalid client address"):
        TrustedProxyHeaders(proxy_response)(request)


def test_mapped_ipv4_addresses_cannot_rotate_rate_limit_identity() -> None:
    assert normalize_client_ip("::ffff:192.0.2.1") == "192.0.2.1"


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
def test_ipv6_addresses_in_same_prefix_share_authentication_rate_limit() -> None:
    first = RequestFactory().post("/", REMOTE_ADDR="2001:db8:1:2::1")
    rotated = RequestFactory().post("/", REMOTE_ADDR="2001:db8:1:2::2")
    other = RequestFactory().post("/", REMOTE_ADDR="2001:db8:1:3::1")
    allowed = [
        bool(consume(request, app="allauth.account", action="login", config={"login": "1/m/ip"}))
        for request in (first, rotated, other)
    ]
    assert allowed == [True, False, True]


def verified_member() -> User:
    user = User.objects.create_user(username="security-member", email="member@example.com")
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("authenticated_at", None),
        ("authenticated_at", "invalid"),
        ("authenticated_at", float("inf")),
        ("authenticated_at", float("nan")),
        ("authenticated_at", 100.0),
        ("last_activity_at", 100.0),
        ("authenticated_at", 10**20),
    ],
)
def test_invalid_or_expired_session_is_revoked(key: str, value: object) -> None:
    client = Client()
    client.force_login(verified_member())
    session = client.session
    session[key] = value
    session.save()
    response = client.get("/api/session/")
    assert response.status_code == 200
    assert response.json()["user"] is None


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
def test_session_activity_refresh_never_extends_absolute_lifetime() -> None:
    client = Client()
    user = verified_member()
    client.force_login(user)
    session = client.session
    started = session["authenticated_at"]
    session["last_activity_at"] = time.time() - 120
    session.save()
    assert client.get("/api/session/").json()["user"]["id"] == user.pk
    assert client.session["last_activity_at"] > time.time() - 60
    assert client.session["authenticated_at"] == started
    assert client.get("/api/session/").json()["user"]["id"] == user.pk


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
def test_mfa_evidence_is_bound_to_current_user_and_live_authenticator() -> None:
    request = RequestFactory().get("/admin/")
    request.user = verified_member()
    request.session = SessionStore()
    assert not has_verified_mfa(request)
    authenticator = Authenticator.objects.create(user=request.user, type="totp", data={})
    record_authentication(request, request.user, method="mfa", type="totp", id=authenticator.pk)
    assert has_verified_mfa(request)
    other = User.objects.create_user(username="other")
    request.user = other
    assert not has_verified_mfa(request)
    request.user = authenticator.user
    authenticator.delete()
    assert not has_verified_mfa(request)
    request.user.is_active = False
    assert not has_verified_mfa(request)


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
def test_admin_has_no_independent_password_entry_point() -> None:
    client = Client()
    assert client.get("/admin/login/").headers["Location"] == "/app/?next=/admin/"
    assert (
        client.post("/admin/login/", {"username": "operator", "password": "secret"}).headers[
            "Location"
        ]
        == "/app/?next=/admin/"
    )


@pytest.mark.parametrize(
    ("path", "status", "logged"),
    [
        ("/api/auth/browser/v1/auth/login", 429, True),
        ("/api/tenants/", 429, False),
        ("/", 200, False),
    ],
)
def test_authentication_throttling_emits_private_security_events(
    path: str, status: int, logged: bool, caplog: pytest.LogCaptureFixture
) -> None:
    request = RequestFactory().post(path, {"password": "never-log-this"})
    with caplog.at_level(logging.INFO, logger="pyracast.security"):
        PrivateAPIResponses(lambda _: HttpResponse(status=status))(request)
    events = [json.loads(record.message) for record in caplog.records]
    assert bool(events) == logged
    if logged:
        assert events == [
            {
                "event": "authentication_request_throttled",
                "user_id": None,
                "client_ip": "127.0.0.1",
            }
        ]
    assert "never-log-this" not in caplog.text


def test_passwords_are_hashed_and_checked_only_with_argon2() -> None:
    assert project_settings.PASSWORD_HASHERS == ["django.contrib.auth.hashers.Argon2PasswordHasher"]
