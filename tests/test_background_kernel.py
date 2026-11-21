import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.db.models import Q

from pyracast.background import clock as monotonic_clock
from pyracast.background import lease
from pyracast.background.clock import Clock
from pyracast.background.retry_policy import CALIBRATION, MAIL, PROVIDER_JOB, RetryPolicy

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
TOKEN = uuid.uuid4()


def row(
    status: str = "running", expires: datetime | None = NOW, attempts: int = 1
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status, attempts=attempts, lease_token=TOKEN, lease_expires_at=expires
    )


def test_a_clock_adds_the_monotonic_time_spent_to_the_time_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = [10.0]
    monkeypatch.setattr(monotonic_clock, "time", SimpleNamespace(monotonic=lambda: ticks[0]))
    clock = Clock(NOW)
    ticks[0] = 12.5

    assert clock.elapsed() == 2.5
    assert clock.now() == NOW + timedelta(seconds=2.5)
    with pytest.raises(ValidationError, match="aware timestamp"):
        Clock(NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("expires", "expired"),
    [
        (None, True),
        (NOW - timedelta(seconds=1), True),
        (NOW, True),
        (NOW + timedelta(seconds=1), False),
    ],
)
def test_a_lease_ends_at_its_expiry(expires: datetime | None, expired: bool) -> None:
    leased = row(expires=expires)

    assert lease.expired(leased, NOW) is expired
    assert lease.held(leased, "running", NOW) is not expired
    assert lease.owns(leased, TOKEN, "running", NOW) is not expired
    assert lease.carries(leased, TOKEN, "running")


def test_only_the_token_and_status_of_the_lease_carry_it() -> None:
    leased = row(expires=NOW + timedelta(minutes=1))

    assert not lease.held(leased, "sending", NOW)
    assert not lease.carries(leased, uuid.uuid4(), "running")
    assert not lease.carries(leased, TOKEN, "sending")
    assert not lease.owns(leased, uuid.uuid4(), "running", NOW)


def test_taking_a_lease_spends_an_attempt_and_releasing_it_clears_both_columns() -> None:
    leased = row(status="pending", expires=None, attempts=2)

    token = lease.take(leased, "running", NOW, timedelta(minutes=5))

    assert (leased.status, leased.attempts, leased.lease_token, leased.lease_expires_at) == (
        "running",
        3,
        token,
        NOW + timedelta(minutes=5),
    )
    lease.release(leased)
    assert (leased.lease_token, leased.lease_expires_at) == (None, None)
    assert dict(lease.RELEASED) == {"lease_token": None, "lease_expires_at": None}


def test_a_lease_is_claimable_once_it_ends_and_held_before() -> None:
    assert lease.claimable("running", NOW) == Q(status="running", lease_expires_at__lte=NOW)
    assert lease.holding("running", NOW) == Q(status="running", lease_expires_at__gt=NOW)


@pytest.mark.parametrize(
    ("policy", "last_retried"), [(PROVIDER_JOB, 2), (CALIBRATION, 2), (MAIL, 7)]
)
def test_each_job_kind_retries_until_its_attempts_are_spent(
    policy: RetryPolicy, last_retried: int
) -> None:
    assert not policy.exhausted(last_retried)
    assert policy.exhausted(last_retried + 1)


@pytest.mark.parametrize(
    ("attempts", "jitter", "delay"), [(1, 0, 60), (5, 30, 990), (6, 0, 1800), (8, 30, 1830)]
)
def test_mail_waits_twice_as_long_after_each_attempt_up_to_half_an_hour(
    monkeypatch: pytest.MonkeyPatch, attempts: int, jitter: int, delay: int
) -> None:
    monkeypatch.setattr("pyracast.background.retry_policy.secrets.randbelow", lambda _: jitter)

    assert MAIL.delay(attempts) == timedelta(seconds=delay)
