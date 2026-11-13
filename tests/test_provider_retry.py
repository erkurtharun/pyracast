from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from pyracast.connections.contracts import ProviderError
from pyracast.connections.operations import (
    RequestDeferredError,
    record_provider_failure,
    reserve_account_request,
)
from pyracast.forecasts.jobs import collect
from pyracast.forecasts.models import CollectionJob, RequestAttempt
from pyracast.provider_retry import next_retry_at, retry_after_seconds
from tests.test_domain import NOW, FakeAdapter, make_system
from tests.test_jobs import calculate


def test_retry_after_two_days_is_not_shortened() -> None:
    assert retry_after_seconds("172800") == 172800
    assert next_retry_at(NOW, 1, 172800) >= NOW + timedelta(days=2)


def test_retry_after_http_date_preserves_long_future_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    class Clock:
        @staticmethod
        def now(zone: object) -> datetime:
            return NOW

    monkeypatch.setattr("pyracast.provider_retry.datetime", Clock)
    assert retry_after_seconds(format_datetime(NOW + timedelta(days=2))) == 172800


@pytest.mark.parametrize("header", ["9" * 128, "Fri, 31 Dec 9999 23:59:59 GMT"])
def test_pathological_hint_saturates_to_the_last_representable_instant(
    monkeypatch: pytest.MonkeyPatch, header: str
) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> Clock:
            return cls(2026, 11, 9, 13, tzinfo=UTC)

    monkeypatch.setattr("pyracast.provider_retry.datetime", Clock)
    monkeypatch.setattr("pyracast.provider_retry.secrets.randbelow", lambda _: 30)
    delay = retry_after_seconds(header)
    assert delay is not None
    retry_at = next_retry_at(NOW, 1, delay)
    assert retry_at == datetime.max.replace(tzinfo=UTC) - timedelta(days=1)
    from zoneinfo import ZoneInfo

    assert retry_at.astimezone(ZoneInfo("Europe/Istanbul")).year == 9999


@pytest.mark.django_db(transaction=True)
def test_long_throttling_hint_blocks_every_new_account_request() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()

    def fail() -> None:
        raise ProviderError("private", status_code=429, retry_after_seconds=172800)

    assert not collect(job.pk, FakeAdapter(fail), calculate, NOW)
    job.refresh_from_db()
    assert job.available_at >= NOW + timedelta(days=2)
    with pytest.raises(RequestDeferredError) as caught:
        reserve_account_request(system.account, NOW + timedelta(days=1), purpose="validation")
    assert caught.value.available_at is not None
    assert caught.value.available_at >= NOW + timedelta(days=2)
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 1


@pytest.mark.django_db(transaction=True)
def test_pathological_throttling_hint_remains_a_durable_safe_deferral() -> None:
    system = make_system()
    delay = retry_after_seconds("9" * 128)
    record_provider_failure(
        system.account, ProviderError("private", status_code=429, retry_after_seconds=delay), NOW
    )
    system.account.refresh_from_db()
    assert system.account.request_blocked_until == datetime.max.replace(tzinfo=UTC) - timedelta(
        days=1
    )
    with pytest.raises(RequestDeferredError):
        reserve_account_request(system.account, NOW + timedelta(days=365), purpose="validation")
