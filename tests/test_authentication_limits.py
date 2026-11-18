import json
import logging
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from threading import Barrier, BrokenBarrierError, Event
from types import SimpleNamespace
from typing import Any

import pytest
from allauth.account.internal import stagekit
from allauth.account.models import EmailAddress
from allauth.core.internal import ratelimit
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal import auth as totp_auth
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections
from django.test import Client, override_settings

from pyracast.accounts.audit import logger as security_logger
from tests.support.auth_lifecycle import AUTH, PASSWORD, mailed_code, member, submit, totp_code
from tests.support.company_registration import signup

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
WRONG = "wrong-" + PASSWORD


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


@dataclass
class Clock:
    now: float

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def read(self) -> float:
        # allauth rolls a hit back by its timestamp, so every reading must be distinct.
        self.now += 1e-6
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock(time.time())
    monkeypatch.setattr(
        ratelimit,
        "time",
        SimpleNamespace(time=clock.read, monotonic=time.monotonic, sleep=time.sleep),
    )
    return clock


@pytest.fixture
def security_log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> pytest.LogCaptureFixture:
    monkeypatch.setattr(security_logger, "handlers", [caplog.handler])
    monkeypatch.setattr(security_logger, "propagate", False)
    caplog.set_level(logging.INFO, logger="pyracast.security")
    return caplog


def security_events(log: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(record.message) for record in log.records if record.name == "pyracast.security"
    ]


def login(client: Client, email: str, password: str) -> Any:
    return submit(client, "auth/login", {"email": email, "password": password})


def error_codes(response: Any) -> list[str]:
    assert response.status_code == 400
    return [error["code"] for error in response.json()["errors"]]


def guess_unknown_addresses(client: Client, first: int, count: int) -> None:
    for index in range(first, first + count):
        assert error_codes(login(client, f"absent-{index}@example.com", WRONG)) == [
            "email_password_mismatch"
        ]


def totp_member(
    monkeypatch: pytest.MonkeyPatch, now: float, user: User | None = None
) -> tuple[User, str]:
    user = user or member()
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now))
    return user, secret


def account(name: str) -> User:
    user = User.objects.create_user(username=name, email=f"{name}@example.com", password=PASSWORD)
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


def pending_second_factor(
    monkeypatch: pytest.MonkeyPatch, now: float, name: str
) -> tuple[Client, str]:
    user, secret = totp_member(monkeypatch, now, account(name))
    client = Client(enforce_csrf_checks=True)
    assert login(client, user.email, PASSWORD).status_code == 401
    return client, secret


def in_parallel(requests: list[Callable[[], Any]]) -> list[Any]:
    start = Barrier(len(requests))

    def run(request: Callable[[], Any]) -> Any:
        close_old_connections()
        try:
            start.wait(timeout=10)
            return request()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        return list(executor.map(run, requests))


def hold_password_checks(
    monkeypatch: pytest.MonkeyPatch, parties: int, success_delay: float = 0.0
) -> None:
    # Every check that starts waits for the others, so all of them run inside one window; a
    # correct password answers after `success_delay` so the failures are handled first.
    checks = Barrier(parties)
    check_password = User.check_password

    def held(user: User, raw_password: str) -> bool:
        with suppress(BrokenBarrierError):
            checks.wait(timeout=2)
        correct = check_password(user, raw_password)
        if correct:
            time.sleep(success_delay)
        return correct

    monkeypatch.setattr(User, "check_password", held)


def hold_correct_password(monkeypatch: pytest.MonkeyPatch) -> tuple[Event, Event]:
    # A correct password check signals `held` after its slots are reserved and answers only
    # once `release` is set.
    held, release = Event(), Event()
    check_password = User.check_password

    def checked(user: User, raw_password: str) -> bool:
        correct = check_password(user, raw_password)
        if correct:
            held.set()
            release.wait(timeout=10)
        return correct

    monkeypatch.setattr(User, "check_password", checked)
    return held, release


def fail_daily(client: Client, email: str, failures: int, clock: Clock) -> None:
    for start in range(0, failures, 5):
        for _ in range(min(5, failures - start)):
            assert error_codes(login(client, email, WRONG)) == ["email_password_mismatch"]
        clock.advance(601)


def test_five_wrong_passwords_lock_the_address_for_ten_minutes(clock: Clock) -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    for _ in range(5):
        assert error_codes(login(client, user.email, WRONG)) == ["email_password_mismatch"]
    assert error_codes(login(client, user.email, PASSWORD)) == ["too_many_login_attempts"]
    clock.advance(601)
    assert login(client, user.email, PASSWORD).status_code == 200


def test_ten_failures_from_one_address_lock_every_login_from_it(clock: Clock) -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    guess_unknown_addresses(client, 0, 10)
    assert error_codes(login(client, user.email, PASSWORD)) == ["too_many_login_attempts"]
    other = Client(enforce_csrf_checks=True, REMOTE_ADDR="198.51.100.7")
    assert login(other, user.email, PASSWORD).status_code == 200


def test_wrong_second_factor_codes_count_towards_the_client_address(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    user, _ = totp_member(monkeypatch, now)
    own = Client(enforce_csrf_checks=True)
    assert login(own, user.email, PASSWORD).status_code == 401
    for _ in range(5):
        assert error_codes(submit(own, "auth/2fa/authenticate", {"code": "000000"})) == [
            "incorrect_code"
        ]
    guesses = Client(enforce_csrf_checks=True)
    guess_unknown_addresses(guesses, 0, 5)
    assert error_codes(login(guesses, "absent-5@example.com", WRONG)) == ["too_many_login_attempts"]


def test_wrong_second_factor_codes_for_several_accounts_share_the_client_allowance(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    signed_in, signed_in_secret = pending_second_factor(monkeypatch, now, "owner")
    assert (
        submit(signed_in, "auth/2fa/authenticate", {"code": totp_code(signed_in_secret, now)})
    ).status_code == 200
    pending = [pending_second_factor(monkeypatch, now, f"member{index}") for index in range(3)]
    codes = [
        error_codes(submit(client, "auth/2fa/authenticate", {"code": "000000"}))[0]
        for _ in range(4)
        for client, _ in pending
    ]
    assert codes == ["incorrect_code"] * 10 + ["too_many_login_attempts"] * 2
    client, secret = pending[0]
    assert error_codes(
        submit(client, "auth/2fa/authenticate", {"code": totp_code(secret, now)})
    ) == ["too_many_login_attempts"]
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now + 31))
    assert error_codes(
        submit(
            signed_in, "auth/2fa/reauthenticate", {"code": totp_code(signed_in_secret, now + 31)}
        )
    ) == ["too_many_login_attempts"]


def test_wrong_reauthentication_codes_count_towards_the_client_allowance(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    client, secret = pending_second_factor(monkeypatch, now, "member")
    assert (
        submit(client, "auth/2fa/authenticate", {"code": totp_code(secret, now)}).status_code == 200
    )
    for _ in range(5):
        assert error_codes(submit(client, "auth/2fa/reauthenticate", {"code": "000000"})) == [
            "incorrect_code"
        ]
    guesses = Client(enforce_csrf_checks=True)
    guess_unknown_addresses(guesses, 0, 5)
    assert error_codes(login(guesses, "absent-5@example.com", WRONG)) == ["too_many_login_attempts"]


def test_five_wrong_second_factor_codes_lock_the_account_code_check(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    client, secret = pending_second_factor(monkeypatch, now, "member")
    for _ in range(5):
        assert error_codes(submit(client, "auth/2fa/authenticate", {"code": "000000"})) == [
            "incorrect_code"
        ]
    assert error_codes(
        submit(client, "auth/2fa/authenticate", {"code": totp_code(secret, now)})
    ) == ["too_many_login_attempts"]


def test_a_pending_second_factor_without_an_authenticator_refuses_the_code(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    client, secret = pending_second_factor(monkeypatch, now, "member")
    Authenticator.objects.all().delete()

    assert error_codes(
        submit(client, "auth/2fa/authenticate", {"code": totp_code(secret, now)})
    ) == ["incorrect_code"]
    assert client.get("/api/session/").json()["user"] is None


def test_an_authenticator_refusal_releases_only_its_own_failure_reservations(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = pending_second_factor(monkeypatch, time.time(), "member")
    guess_unknown_addresses(client, 0, 5)
    for _ in range(4):
        assert error_codes(submit(client, "auth/2fa/authenticate", {"code": "000000"})) == [
            "incorrect_code"
        ]

    def unavailable(authenticator: TOTP, code: str) -> bool:
        raise ValidationError("The authenticator is temporarily unavailable.", code="rate_limited")

    with monkeypatch.context() as validation:
        validation.setattr(TOTP, "validate_code", unavailable)
        assert error_codes(submit(client, "auth/2fa/authenticate", {"code": "000000"})) == [
            "rate_limited"
        ]

    assert error_codes(submit(client, "auth/2fa/authenticate", {"code": "000000"})) == [
        "incorrect_code"
    ]
    assert error_codes(login(client, "absent-5@example.com", WRONG)) == ["too_many_login_attempts"]
    client.defaults["REMOTE_ADDR"] = "198.51.100.7"
    assert error_codes(submit(client, "auth/2fa/authenticate", {"code": "000000"})) == [
        "too_many_login_attempts"
    ]


def refuse_codes_before_the_first_daily_failures_expire(
    client: Client, path: str, refusal: str, clock: Clock
) -> None:
    # 100 wrong codes spread over 10-minute windows, then five attempts refused a minute before
    # the oldest of them leaves the day; on return the daily allowance has room again.
    start = clock.now
    for _ in range(20):
        for _ in range(5):
            assert error_codes(submit(client, path, {"code": "000000"})) == ["incorrect_code"]
        clock.advance(601)
    clock.advance(start + 24 * 3600 - 60 - clock.now)
    for _ in range(5):
        assert error_codes(submit(client, path, {"code": "000000"})) == [refusal]
    clock.advance(61)


@pytest.mark.parametrize("path", ["auth/2fa/authenticate", "auth/2fa/reauthenticate"])
def test_refused_codes_just_before_daily_failures_expire_leave_the_short_window_free(
    path: str, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    client, secret = pending_second_factor(monkeypatch, now, "member")
    code_time = now
    if path == "auth/2fa/reauthenticate":
        assert (
            submit(client, "auth/2fa/authenticate", {"code": totp_code(secret, now)}).status_code
            == 200
        )
        code_time = now + 31
        monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: code_time))
    refuse_codes_before_the_first_daily_failures_expire(
        client, path, "too_many_login_attempts", clock
    )
    assert submit(client, path, {"code": totp_code(secret, code_time)}).status_code == 200


def test_refused_enrollment_codes_just_before_daily_failures_expire_leave_the_short_window_free(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now))
    user = member()
    client = Client(enforce_csrf_checks=True)
    assert login(client, user.email, PASSWORD).status_code == 200
    secret = client.get(AUTH + "account/authenticators/totp").json()["meta"]["secret"]
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    refuse_codes_before_the_first_daily_failures_expire(
        client, "account/authenticators/totp", "rate_limited", clock
    )
    response = submit(client, "account/authenticators/totp", {"code": totp_code(secret, now)})
    assert response.status_code == 200


@pytest.mark.parametrize(("elapsed", "status"), [(899, 200), (901, 401)])
def test_a_pending_second_factor_stage_expires_after_fifteen_minutes(
    elapsed: int, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    client, secret = pending_second_factor(monkeypatch, now, "member")
    monkeypatch.setattr(stagekit, "time", SimpleNamespace(time=lambda: now + elapsed))
    response = submit(client, "auth/2fa/authenticate", {"code": totp_code(secret, now)})
    assert response.status_code == status


def test_an_unverified_address_is_verified_before_the_second_factor_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = time.time()
    user = User.objects.create_user(
        username="member", email="member@example.com", password=PASSWORD
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=False)
    _, secret = totp_member(monkeypatch, now, user)
    client = Client(enforce_csrf_checks=True)

    def pending_flows(response: Any) -> list[str]:
        assert response.status_code == 401
        return [flow["id"] for flow in response.json()["data"]["flows"] if flow.get("is_pending")]

    assert pending_flows(login(client, user.email, PASSWORD)) == ["verify_email"]
    verified = submit(client, "auth/email/verify", {"key": mailed_code()})
    assert pending_flows(verified) == ["mfa_authenticate"]
    response = submit(client, "auth/2fa/authenticate", {"code": totp_code(secret, now)})
    assert response.status_code == 200


def test_concurrent_password_checks_from_one_client_stay_within_its_allowance(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = member()
    guess_unknown_addresses(Client(enforce_csrf_checks=True), 0, 9)
    hold_password_checks(monkeypatch, 4)
    responses = in_parallel(
        [lambda: login(Client(enforce_csrf_checks=True), user.email, WRONG)] * 4
    )
    assert (
        sorted(error_codes(response)[0] for response in responses)
        == ["email_password_mismatch"] + ["too_many_login_attempts"] * 3
    )


@pytest.mark.parametrize("success", ["totp_login", "totp_reauthentication", "password_reset"])
def test_a_successful_sign_in_step_keeps_the_failures_counted_for_the_client_address(
    success: str, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    user, secret = totp_member(monkeypatch, now)
    guesses = Client(enforce_csrf_checks=True)
    own = Client(enforce_csrf_checks=True)
    if success == "totp_login":
        guess_unknown_addresses(guesses, 0, 9)
    assert login(own, user.email, PASSWORD).status_code == 401
    assert submit(own, "auth/2fa/authenticate", {"code": totp_code(secret, now)}).status_code == 200
    if success == "totp_reauthentication":
        guess_unknown_addresses(guesses, 0, 9)
        monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now + 31))
        assert (
            submit(
                own, "auth/2fa/reauthenticate", {"code": totp_code(secret, now + 31)}
            ).status_code
            == 200
        )
    if success == "password_reset":
        guess_unknown_addresses(guesses, 0, 9)
        reset = Client(enforce_csrf_checks=True)
        assert submit(reset, "auth/password/request", {"email": user.email}).status_code == 401
        assert (
            submit(
                reset, "auth/password/reset", {"key": mailed_code(), "password": PASSWORD + "new"}
            ).status_code
            == 401
        )
    guess_unknown_addresses(guesses, 9, 1)
    assert error_codes(login(guesses, "absent-10@example.com", WRONG)) == [
        "too_many_login_attempts"
    ]


@pytest.mark.parametrize("refusal", ["staff_without_totp", "inactive"])
def test_a_correct_password_of_a_refused_account_answers_and_counts_as_a_failure(
    refusal: str, clock: Clock
) -> None:
    user = member()
    if refusal == "staff_without_totp":
        user.is_staff = True
    else:
        user.is_active = False
    user.save(update_fields=["is_staff", "is_active"])
    client = Client(enforce_csrf_checks=True)
    for _ in range(5):
        assert error_codes(login(client, user.email, PASSWORD)) == ["email_password_mismatch"]
    assert error_codes(login(client, user.email, PASSWORD)) == ["too_many_login_attempts"]
    assert client.get("/api/session/").json()["user"] is None


def test_a_refused_login_records_one_login_locked_event_without_the_address(
    clock: Clock, security_log: pytest.LogCaptureFixture
) -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    for _ in range(5):
        assert error_codes(login(client, user.email, WRONG)) == ["email_password_mismatch"]
    security_log.clear()
    assert error_codes(login(client, user.email, PASSWORD)) == ["too_many_login_attempts"]
    [line] = security_events(security_log)
    assert len(line.pop("subject")) == 16
    assert line == {"event": "login_locked", "user_id": None, "client_ip": "127.0.0.1"}
    assert user.email not in security_log.text


def test_a_locked_second_factor_check_is_recorded(
    clock: Clock, monkeypatch: pytest.MonkeyPatch, security_log: pytest.LogCaptureFixture
) -> None:
    client, _ = pending_second_factor(monkeypatch, time.time(), "member")
    user = User.objects.get(username="member")
    for _ in range(5):
        assert error_codes(submit(client, "auth/2fa/authenticate", {"code": "000000"})) == [
            "incorrect_code"
        ]
    security_log.clear()
    assert error_codes(submit(client, "auth/2fa/authenticate", {"code": "000000"})) == [
        "too_many_login_attempts"
    ]
    assert security_events(security_log) == [
        {"event": "second_factor_locked", "user_id": user.pk, "client_ip": "127.0.0.1"}
    ]


def test_a_hundred_failures_in_a_day_lock_the_address_until_a_password_reset(
    clock: Clock,
) -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    fail_daily(client, user.email, 100, clock)
    for _ in range(2):
        assert error_codes(login(client, user.email, PASSWORD)) == ["too_many_login_attempts"]
    assert not mail.outbox
    reset = Client(enforce_csrf_checks=True)
    assert submit(reset, "auth/password/request", {"email": user.email}).status_code == 401
    assert (
        submit(
            reset, "auth/password/reset", {"key": mailed_code(), "password": PASSWORD + "new"}
        ).status_code
        == 401
    )
    assert login(client, user.email, PASSWORD + "new").status_code == 200


def test_a_success_beside_the_last_daily_failure_does_not_lock(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = member()
    fail_daily(Client(enforce_csrf_checks=True), user.email, 98, clock)
    hold_password_checks(monkeypatch, 2, success_delay=1.0)
    statuses = in_parallel(
        [
            lambda: login(Client(enforce_csrf_checks=True), user.email, WRONG).status_code,
            lambda: login(Client(enforce_csrf_checks=True), user.email, PASSWORD).status_code,
        ]
    )
    assert statuses == [400, 200]
    assert login(Client(enforce_csrf_checks=True), user.email, PASSWORD).status_code == 200


def test_a_refusal_beside_a_pending_correct_password_leaves_the_quota_as_it_was(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = member()
    fail_daily(Client(enforce_csrf_checks=True), user.email, 98, clock)
    held, release = hold_correct_password(monkeypatch)

    def beside_the_held_check() -> list[list[str]]:
        assert held.wait(timeout=10)
        try:
            return [
                error_codes(login(Client(enforce_csrf_checks=True), user.email, WRONG))
                for _ in range(2)
            ]
        finally:
            release.set()

    success, codes = in_parallel(
        [
            lambda: login(Client(enforce_csrf_checks=True), user.email, PASSWORD).status_code,
            beside_the_held_check,
        ]
    )
    assert success == 200
    assert codes == [["email_password_mismatch"], ["too_many_login_attempts"]]
    client = Client(enforce_csrf_checks=True)
    assert error_codes(login(client, user.email, WRONG)) == ["email_password_mismatch"]
    assert error_codes(login(client, user.email, PASSWORD)) == ["too_many_login_attempts"]


def test_refusals_just_before_daily_failures_expire_leave_the_short_window_free(
    clock: Clock,
) -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    start = clock.now
    fail_daily(client, user.email, 100, clock)
    clock.advance(start + 24 * 3600 - 60 - clock.now)
    for _ in range(5):
        assert error_codes(login(client, user.email, WRONG)) == ["too_many_login_attempts"]
    clock.advance(61)
    assert login(client, user.email, PASSWORD).status_code == 200


def test_an_unknown_address_is_locked_after_a_hundred_failures_in_a_day(clock: Clock) -> None:
    client = Client(enforce_csrf_checks=True)
    fail_daily(client, "absent@example.com", 100, clock)
    assert error_codes(login(client, "absent@example.com", WRONG)) == ["too_many_login_attempts"]
    assert not mail.outbox


def test_a_signed_in_client_is_refused_before_its_login_input_is_checked(clock: Clock) -> None:
    user = member()
    other = User.objects.create_user(username="other", email="other@example.com", password=PASSWORD)
    EmailAddress.objects.create(user=other, email=other.email, primary=True, verified=True)
    client = Client(enforce_csrf_checks=True)
    assert login(client, user.email, PASSWORD).status_code == 200
    for _ in range(6):
        assert login(client, other.email, WRONG).status_code == 409
    assert login(Client(enforce_csrf_checks=True), other.email, PASSWORD).status_code == 200


def visitor(index: int) -> Client:
    return Client(enforce_csrf_checks=True, REMOTE_ADDR=f"198.51.100.{index + 1}")


def company(index: int) -> dict[str, str]:
    return {"company_name": f"Sunfield Energy {index}"}


def test_an_address_receives_one_verification_mail_per_minute(clock: Clock) -> None:
    assert signup(visitor(0), company(0)).status_code == 401
    clock.advance(59)
    assert signup(visitor(1), company(1)).status_code == 429
    clock.advance(2)
    assert signup(visitor(2), company(2)).status_code == 401
    assert len(mail.outbox) == 2


def test_an_address_receives_at_most_six_verification_mails_an_hour(clock: Clock) -> None:
    statuses = []
    for index in range(7):
        statuses.append(signup(visitor(index), company(index)).status_code)
        clock.advance(181)
    assert statuses == [401] * 6 + [429]
    assert len(mail.outbox) == 6


def test_wrong_current_passwords_are_limited_per_ten_minutes_and_recorded(
    clock: Clock, security_log: pytest.LogCaptureFixture
) -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    assert (
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200
    )
    security_log.clear()
    change = {"current_password": "wrong-" + PASSWORD, "new_password": PASSWORD + "new"}
    for _ in range(5):
        assert submit(client, "account/password/change", change).status_code == 400
        clock.advance(61)
    assert submit(client, "account/password/change", change).status_code == 429
    assert security_events(security_log) == [
        {"event": "password_change_failed", "user_id": user.pk, "client_ip": "127.0.0.1"}
    ] * 5 + [
        {"event": "authentication_request_throttled", "user_id": None, "client_ip": "127.0.0.1"}
    ]
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


@pytest.mark.parametrize(("elapsed", "status"), [(599, 429), (601, 400)])
def test_password_change_allowance_reopens_after_ten_minutes(
    elapsed: int, status: int, clock: Clock
) -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    assert login(client, user.email, PASSWORD).status_code == 200
    change = {"current_password": WRONG, "new_password": PASSWORD + "new"}
    for _ in range(5):
        assert submit(client, "account/password/change", change).status_code == 400

    clock.advance(elapsed)
    assert submit(client, "account/password/change", change).status_code == status
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


def test_provider_signup_without_a_pending_signup_leaves_the_signup_quota(clock: Clock) -> None:
    client = Client(enforce_csrf_checks=True)
    for _ in range(5):
        assert submit(client, "auth/provider/signup", {}).status_code == 409
    assert signup(client).status_code == 401
