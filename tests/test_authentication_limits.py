import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.core.internal import ratelimit
from allauth.mfa.totp.internal import auth as totp_auth
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, override_settings

from pyracast.accounts.audit import logger as security_logger
from tests.test_auth_lifecycle import PASSWORD, mailed_code, member, submit, totp_code
from tests.test_company_registration import signup

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


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock(time.time())
    monkeypatch.setattr(
        ratelimit,
        "time",
        SimpleNamespace(time=lambda: clock.now, monotonic=time.monotonic, sleep=time.sleep),
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


def totp_member(monkeypatch: pytest.MonkeyPatch, now: float) -> tuple[User, str]:
    user = member()
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now))
    return user, secret


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
    assert security_events(security_log) == [
        {"event": "login_locked", "user_id": None, "client_ip": "127.0.0.1"}
    ]
    assert user.email not in security_log.text


def test_the_hundredth_failure_in_a_day_locks_the_address_until_a_password_reset(
    clock: Clock,
) -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    for _ in range(20):
        for _ in range(5):
            assert error_codes(login(client, user.email, WRONG)) == ["email_password_mismatch"]
        clock.advance(601)
    assert [message.to for message in mail.outbox] == [[user.email]]
    assert "reset your password" in str(mail.outbox[0].body)
    for _ in range(2):
        assert error_codes(login(client, user.email, PASSWORD)) == ["too_many_login_attempts"]
    assert len(mail.outbox) == 1
    reset = Client(enforce_csrf_checks=True)
    assert submit(reset, "auth/password/request", {"email": user.email}).status_code == 401
    assert (
        submit(
            reset, "auth/password/reset", {"key": mailed_code(), "password": PASSWORD + "new"}
        ).status_code
        == 401
    )
    assert login(client, user.email, PASSWORD + "new").status_code == 200


def test_the_daily_lock_notice_is_not_sent_for_an_unknown_address(clock: Clock) -> None:
    client = Client(enforce_csrf_checks=True)
    for _ in range(20):
        for _ in range(5):
            assert error_codes(login(client, "absent@example.com", WRONG)) == [
                "email_password_mismatch"
            ]
        clock.advance(601)
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


def test_an_address_receives_one_verification_mail_per_three_minutes(clock: Clock) -> None:
    assert signup(visitor(0), company(0)).status_code == 401
    clock.advance(60)
    assert signup(visitor(1), company(1)).status_code == 429
    clock.advance(121)
    assert signup(visitor(2), company(2)).status_code == 401
    assert len(mail.outbox) == 2


def test_an_address_receives_at_most_six_verification_mails_an_hour(clock: Clock) -> None:
    statuses = []
    for index in range(7):
        statuses.append(signup(visitor(index), company(index)).status_code)
        clock.advance(181)
    assert statuses == [401] * 6 + [429]
    assert len(mail.outbox) == 6


def test_anonymous_account_mails_share_one_hourly_budget(
    clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="pyracast.accounts.adapters")
    responses = [
        submit(visitor(index), "auth/password/request", {"email": f"absent-{index}@example.com"})
        for index in range(21)
    ]
    assert {response.status_code for response in responses} == {401}
    assert all(response.json() == responses[0].json() for response in responses)
    assert len(mail.outbox) == 20
    assert "account_mail_skipped template=account/email/unknown_account" in caplog.text
    assert "absent-20@example.com" not in caplog.text
    clock.advance(3601)
    assert (
        submit(visitor(21), "auth/password/request", {"email": "absent-21@example.com"}).status_code
        == 401
    )
    assert len(mail.outbox) == 21


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
    assert (
        security_events(security_log)
        == [{"event": "password_change_failed", "user_id": user.pk, "client_ip": "127.0.0.1"}] * 5
    )
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


def test_provider_signup_without_a_pending_signup_leaves_the_signup_quota(clock: Clock) -> None:
    client = Client(enforce_csrf_checks=True)
    for _ in range(5):
        assert submit(client, "auth/provider/signup", {}).status_code == 409
    assert signup(client).status_code == 401
