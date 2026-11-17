import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local
from types import SimpleNamespace
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.core.internal import ratelimit
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal import auth as totp_auth
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.auth.models import User
from django.core import mail
from django.db import close_old_connections, connections
from django.test import Client, override_settings

from tests.support.auth_lifecycle import AUTH, PASSWORD, mailed_code, member, submit, totp_code

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def signup_unverified() -> Any:
    details = {
        "company_name": "Sunfield Energy",
        "email": "owner@example.com",
        "password": PASSWORD,
    }
    return submit(Client(enforce_csrf_checks=True), "auth/signup", details)


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "en",
            "Too many sign-in attempts, or a verification code was sent recently. "
            "Try again in a few minutes.",
        ),
        (
            "tr",
            "Çok fazla giriş denemesi yap\u0131ld\u0131 veya k\u0131sa süre önce bir doğrulama "
            "kodu gönderildi. Birkaç dakika sonra tekrar deneyin.",
        ),
    ],
)
def test_login_lockout_message_also_covers_a_recent_code(language: str, expected: str) -> None:
    assert signup_unverified().status_code == 401
    client = Client(enforce_csrf_checks=True, HTTP_ACCEPT_LANGUAGE=language)
    response = submit(client, "auth/login", {"email": "owner@example.com", "password": PASSWORD})
    assert response.status_code == 400
    assert [error["message"] for error in response.json()["errors"]] == [expected]


def test_unverified_sign_in_can_request_a_new_code_after_one_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert signup_unverified().status_code == 401
    later = time.time() + 61
    monkeypatch.setattr(
        ratelimit,
        "time",
        SimpleNamespace(time=lambda: later, monotonic=time.monotonic, sleep=time.sleep),
    )
    response = submit(
        Client(enforce_csrf_checks=True),
        "auth/login",
        {"email": "owner@example.com", "password": PASSWORD},
    )
    assert response.status_code == 401
    assert {"id": "verify_email", "is_pending": True} in response.json()["data"]["flows"]
    assert len(mail.outbox) == 2


def test_session_projection_cannot_change_the_account_email() -> None:
    user = User.objects.create_user(username="member", email="original@example.com")
    client = Client()
    client.force_login(user)

    response = client.patch(
        "/api/session/", {"email": "replacement@example.com"}, content_type="application/json"
    )

    assert response.status_code == 405
    user.refresh_from_db()
    assert user.email == "original@example.com"


def test_old_password_entrypoints_are_removed() -> None:
    client = Client()
    for path in ("/auth/login/", "/api/session/login/", "/api/session/register/"):
        assert client.post(path, {}, content_type="application/json").status_code == 404
    for path in (
        "/api/auth/app/v1/auth/session",
        "/api/auth/app/v1/auth/login",
        "/api/auth/app/v1/tokens/refresh",
    ):
        assert client.post(path, {}, content_type="application/json").status_code == 404


def test_password_whitespace_is_preserved_across_login_reauthentication_and_change() -> None:
    user = member()
    password = "  " + PASSWORD + "  "
    user.set_password(password)
    user.save(update_fields=["password"])
    client = Client(enforce_csrf_checks=True)
    assert (
        submit(client, "auth/login", {"email": user.email, "password": password}).status_code == 200
    )
    assert submit(client, "auth/reauthenticate", {"password": password}).status_code == 200
    changed = submit(
        client,
        "account/password/change",
        {"current_password": password, "new_password": password + "new  "},
    )
    assert changed.status_code == 401
    user.refresh_from_db()
    assert user.check_password(password + "new  ")
    assert not user.check_password((password + "new  ").strip())


def test_password_change_requires_at_least_twelve_characters() -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    assert (
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200
    )
    rejected = submit(
        client,
        "account/password/change",
        {"current_password": PASSWORD, "new_password": "aB!57mQ8nP4"},
    )
    assert rejected.status_code == 400
    assert any(error["code"] == "password_too_short" for error in rejected.json()["errors"])
    user.refresh_from_db()
    assert user.check_password(PASSWORD)

    changed = submit(
        client,
        "account/password/change",
        {"current_password": PASSWORD, "new_password": "aB!57mQ8nP4z"},
    )
    assert changed.status_code == 401
    user.refresh_from_db()
    assert user.check_password("aB!57mQ8nP4z")


def test_oversized_login_password_is_rejected_before_hashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_hash(self: User, raw_password: str | None) -> None:
        raise AssertionError("Oversized input reached the password hasher.")

    monkeypatch.setattr(User, "set_password", unexpected_hash)
    client = Client(enforce_csrf_checks=True)
    assert (
        submit(
            client, "auth/login", {"email": "missing@example.com", "password": "x" * 1025}
        ).status_code
        == 400
    )


def test_password_reset_is_generic_session_bound_single_use_and_revokes_sessions() -> None:
    user = member()
    client, reset = Client(enforce_csrf_checks=True), Client(enforce_csrf_checks=True)
    assert (
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200
    )
    known = submit(reset, "auth/password/request", {"email": user.email})
    code = mailed_code()
    unknown = submit(
        Client(enforce_csrf_checks=True), "auth/password/request", {"email": "missing@example.com"}
    )
    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json()
    assert any(
        flow["id"] == "password_reset_by_code"
        for flow in reset.get(AUTH + "auth/session").json()["data"]["flows"]
    )
    assert (
        submit(
            Client(enforce_csrf_checks=True),
            "auth/password/reset",
            {"key": code, "password": PASSWORD + "new"},
        ).status_code
        == 409
    )
    assert (
        reset.get(AUTH + "auth/password/reset", HTTP_X_PASSWORD_RESET_KEY=code).status_code == 405
    )
    user.refresh_from_db()
    assert user.check_password(PASSWORD)
    changed = submit(reset, "auth/password/reset", {"key": code, "password": PASSWORD + "new"})
    assert changed.status_code == 401
    assert reset.get("/api/session/").json()["user"] is None
    assert client.get("/api/session/").json()["user"] is None
    assert (
        submit(
            reset, "auth/password/reset", {"key": code, "password": PASSWORD + "again"}
        ).status_code
        == 409
    )
    assert (
        submit(reset, "auth/login", {"email": user.email, "password": PASSWORD + "new"}).status_code
        == 200
    )


def test_password_reset_requires_at_least_twelve_characters() -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    code = mailed_code()

    rejected = submit(client, "auth/password/reset", {"key": code, "password": "aB!57mQ8nP4"})
    assert rejected.status_code == 400
    assert any(error["code"] == "password_too_short" for error in rejected.json()["errors"])
    user.refresh_from_db()
    assert user.check_password(PASSWORD)

    changed = submit(client, "auth/password/reset", {"key": code, "password": "aB!57mQ8nP4z"})
    assert changed.status_code == 401
    user.refresh_from_db()
    assert user.check_password("aB!57mQ8nP4z")


def test_expired_reset_code_cannot_change_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    code = mailed_code()
    expired_at = time.time() + 601
    monkeypatch.setattr(time, "time", lambda: expired_at)
    response = submit(client, "auth/password/reset", {"key": code, "password": PASSWORD + "new"})
    assert response.status_code == 409
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


def test_email_change_requires_reauthentication_and_initiating_session_verification() -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    assert (
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200
    )
    replacement = {"email": "new@example.com"}
    assert submit(client, "account/email", replacement).status_code == 401
    assert submit(client, "auth/reauthenticate", {"password": "wrong"}).status_code == 400
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    assert submit(client, "account/email", replacement).status_code == 200
    code = mailed_code()
    user.refresh_from_db()
    assert user.email == "member@example.com"
    assert not EmailAddress.objects.filter(user=user, email=replacement["email"]).exists()
    assert (
        submit(Client(enforce_csrf_checks=True), "auth/email/verify", {"key": code}).status_code
        == 409
    )
    assert submit(client, "auth/email/verify", {"key": code}).status_code == 200
    user.refresh_from_db()
    assert user.email == replacement["email"]
    assert EmailAddress.objects.get(user=user).verified
    assert any(message.to == ["member@example.com"] for message in mail.outbox)


def test_pending_email_change_cannot_be_used_for_login_or_recovery() -> None:
    user = member()
    pending = "unverified-owner@example.com"
    EmailAddress.objects.create(user=user, email=pending)
    client = Client(enforce_csrf_checks=True)
    response = submit(client, "auth/login", {"email": pending, "password": PASSWORD})
    assert response.status_code == 400
    assert client.get("/api/session/").json()["user"] is None
    response = submit(client, "auth/password/request", {"email": pending})
    assert response.status_code in {200, 401}
    assert not any("Password Reset" in str(message.subject) for message in mail.outbox)


def test_legacy_unverified_email_is_not_an_account_recovery_channel() -> None:
    for index in range(2):
        User.objects.create_user(
            username=f"legacy-{index}", email="legacy@example.com", password=PASSWORD
        )
    response = submit(
        Client(enforce_csrf_checks=True), "auth/password/request", {"email": "legacy@example.com"}
    )
    assert response.status_code in {200, 401}
    assert not any("Password Reset" in str(message.subject) for message in mail.outbox)


def test_ambiguous_unverified_addresses_are_recoverable_but_not_login_identities() -> None:
    for index in range(2):
        user = User.objects.create_user(
            username=f"pending-{index}", email="shared@example.com", password=PASSWORD
        )
        EmailAddress.objects.create(user=user, email=user.email, primary=True)
    client = Client(enforce_csrf_checks=True)
    assert (
        submit(
            client, "auth/login", {"email": "shared@example.com", "password": PASSWORD}
        ).status_code
        == 400
    )
    assert (
        submit(client, "auth/password/request", {"email": "shared@example.com"}).status_code == 401
    )
    assert any("Password Reset" in str(message.subject) for message in mail.outbox)


@pytest.mark.parametrize("state", ["duplicate_verified", "multiple_unverified", "inactive"])
def test_unsafe_email_identity_states_do_not_receive_reset_codes(state: str) -> None:
    user = User.objects.create_user(
        username="identity",
        email="owner@example.com",
        password=PASSWORD,
        is_active=state != "inactive",
    )
    EmailAddress.objects.create(
        user=user, email=user.email, primary=True, verified=state != "multiple_unverified"
    )
    if state == "duplicate_verified":
        other = User.objects.create_user(username="duplicate", email="OWNER@example.com")
        EmailAddress.objects.create(user=other, email=other.email, primary=True, verified=True)
    elif state == "multiple_unverified":
        EmailAddress.objects.create(user=user, email="second@example.com")
    client = Client(enforce_csrf_checks=True)
    response = submit(client, "auth/password/request", {"email": user.email})
    assert response.status_code == 401
    assert not any("Password Reset" in str(message.subject) for message in mail.outbox)


def test_reset_of_initial_signup_address_proves_ownership_and_replaces_attacker_password() -> None:
    user = User.objects.create_user(
        username="initial", email="owner@example.com", password=PASSWORD
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True)
    owner = Client(enforce_csrf_checks=True)
    assert submit(owner, "auth/password/request", {"email": user.email}).status_code == 401
    code = mailed_code()
    assert (
        submit(
            owner, "auth/password/reset", {"key": code, "password": PASSWORD + "owned"}
        ).status_code
        == 401
    )
    assert EmailAddress.objects.get(user=user).verified
    assert (
        submit(owner, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 400
    )
    assert (
        submit(
            owner, "auth/login", {"email": user.email, "password": PASSWORD + "owned"}
        ).status_code
        == 200
    )


def test_reset_code_attempt_limit_aborts_the_process() -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    code = mailed_code()
    for _ in range(3):
        assert (
            submit(
                client, "auth/password/reset", {"key": "WRONG-CODE", "password": PASSWORD + "new"}
            ).status_code
            == 400
        )
    assert (
        submit(
            client, "auth/password/reset", {"key": code, "password": PASSWORD + "new"}
        ).status_code
        == 409
    )
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


def test_verified_address_takes_precedence_over_another_pending_identity() -> None:
    owner = member()
    attacker = User.objects.create_user(
        username="attacker", email=owner.email, password=PASSWORD + "attacker"
    )
    EmailAddress.objects.create(user=attacker, email=owner.email, primary=True)
    client = Client(enforce_csrf_checks=True)
    assert (
        submit(
            client, "auth/login", {"email": owner.email, "password": PASSWORD + "attacker"}
        ).status_code
        == 400
    )
    assert (
        submit(client, "auth/login", {"email": owner.email, "password": PASSWORD}).status_code
        == 200
    )


def test_concurrent_email_confirmations_admit_only_one_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending: list[tuple[Client, str]] = []
    for index in range(2):
        user = User.objects.create_user(
            username=f"member-{index}", email=f"old-{index}@example.com", password=PASSWORD
        )
        EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
        client = Client(enforce_csrf_checks=True)
        assert (
            submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code
            == 200
        )
        assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
        with override_settings(ACCOUNT_RATE_LIMITS={"confirm_email": "100/m/key"}):
            assert (
                submit(client, "account/email", {"email": "shared@example.com"}).status_code == 200
            )
        pending.append((client, mailed_code()))
    barrier = Barrier(2)
    thread = local()
    original = EmailAddress.can_set_verified

    def synchronized_validation(address: EmailAddress) -> bool:
        result = original(address)
        thread.checks = getattr(thread, "checks", 0) + 1
        if thread.checks == 2:
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(EmailAddress, "can_set_verified", synchronized_validation)

    def confirm(value: tuple[Client, str]) -> int:
        close_old_connections()
        try:
            client, code = value
            return int(submit(client, "auth/email/verify", {"key": code}).status_code)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(confirm, pending))
    assert sorted(outcomes) == [200, 400]
    assert EmailAddress.objects.filter(email="shared@example.com", verified=True).count() == 1


def test_totp_and_recovery_codes_gate_login_and_cannot_be_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 200
    setup = client.get(AUTH + "account/authenticators/totp")
    assert setup.status_code == 404
    now = time.time()
    # Reauthentication and CSRF requests can cross the 30-second TOTP boundary.
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now))
    assert (
        submit(
            client,
            "account/authenticators/totp",
            {"code": totp_code(setup.json()["meta"]["secret"], now)},
        ).status_code
        == 401
    )
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    assert (
        submit(
            client,
            "account/authenticators/totp",
            {"code": totp_code(setup.json()["meta"]["secret"], now)},
        ).status_code
        == 200
    )
    assert client.get(AUTH + "account/authenticators/recovery-codes").status_code == 401
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now + 31))
    assert (
        submit(
            client,
            "auth/2fa/reauthenticate",
            {"code": totp_code(setup.json()["meta"]["secret"], now + 31)},
        ).status_code
        == 200
    )
    recovery = client.get(AUTH + "account/authenticators/recovery-codes")
    codes = recovery.json()["data"]["unused_codes"]
    assert codes
    assert (
        "unused_codes"
        not in client.get(AUTH + "account/authenticators/recovery-codes").json()["data"]
    )
    token = client.get("/api/session/").json()["csrf_token"]
    client.delete(AUTH + "auth/session", HTTP_X_CSRFTOKEN=token)
    pending = submit(client, "auth/login", credentials)
    assert pending.status_code == 401
    assert any(flow["id"] == "mfa_authenticate" for flow in pending.json()["data"]["flows"])
    assert client.get("/api/tenants/").status_code == 401
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now + 62))
    assert (
        submit(
            client,
            "auth/2fa/authenticate",
            {"code": totp_code(setup.json()["meta"]["secret"], now + 62)},
        ).status_code
        == 200
    )
    for expected in (200, 400):
        token = client.get("/api/session/").json()["csrf_token"]
        client.delete(AUTH + "auth/session", HTTP_X_CSRFTOKEN=token)
        assert submit(client, "auth/login", credentials).status_code == 401
        if expected == 200:
            assert (
                submit(
                    client,
                    "auth/2fa/authenticate",
                    {"code": totp_code(setup.json()["meta"]["secret"], now + 62)},
                ).status_code
                == 400
            )
        assert submit(client, "auth/2fa/authenticate", {"code": codes[0]}).status_code == expected


@pytest.mark.parametrize("method", ["totp", "recovery_codes"])
def test_eight_parallel_sessions_can_use_a_second_factor_only_once(
    method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = member()
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    recovery = RecoveryCodes.activate(user)
    clients = [Client(enforce_csrf_checks=True) for _ in range(8)]
    for client in clients:
        assert (
            submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code
            == 401
        )
    verification_at = time.time()
    # The real PostgreSQL single-use race must not depend on crossing a TOTP period.
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: verification_at))
    code = totp_code(secret, verification_at) if method == "totp" else recovery.generate_codes()[0]
    barrier = Barrier(8)

    def authenticate(client: Client) -> int:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return int(submit(client, "auth/2fa/authenticate", {"code": code}).status_code)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(authenticate, clients))
    assert outcomes.count(200) == 1
    assert all(status in {200, 400, 429} for status in outcomes)
