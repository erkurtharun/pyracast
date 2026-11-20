from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import psycopg.errors
import pytest
from django.db import DatabaseError, OperationalError

from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.secrets import encrypt_secret
from pyracast.forecasts import jobs, runtime
from pyracast.forecasts.jobs import claim, collect, pending_job_ids
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastSnapshot,
    RequestAttempt,
    RequestPurpose,
)
from pyracast.forecasts.quota import RequestDeferredError, reserve_request
from pyracast.providers import SolcastProvider
from pyracast.weather import OpenMeteoProvider
from pyracast.weather_data import WeatherError
from tests.support.domain import NOW, FakeAdapter, System, make_system
from tests.support.jobs import calculate
from tests.support.providers import TARGET, binding_for

PROVIDER_DAY_RESET = datetime(2026, 11, 10, 0, 2, tzinfo=UTC)
Handler = Callable[[httpx.Request], httpx.Response]


def solcast(handler: Handler) -> SolcastProvider:
    return SolcastProvider(httpx.MockTransport(handler), clock=lambda: NOW)


def keyed_system() -> System:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(
        encrypted_api_key=encrypt_secret("test-credential")
    )
    return system


def answer(status: int) -> Handler:
    return lambda request: httpx.Response(status, json={"response_status": "private"})


def unreachable(request: httpx.Request) -> httpx.Response:
    pytest.fail("A local configuration failure must not reach the provider")


def lock_timeout() -> OperationalError:
    error = OperationalError("canceling statement due to lock timeout")
    error.__cause__ = psycopg.errors.LockNotAvailable()
    return error


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("status", "job_status", "code", "attempts"),
    [
        (401, "failed", "provider_authentication_failed", 1),
        (402, "retry", "provider_allowance_exhausted", 0),
        (403, "failed", "provider_access_denied", 1),
        (404, "failed", "provider_site_not_found", 1),
        (418, "failed", "provider_failure", 1),
        (429, "retry", "provider_throttled", 1),
        (500, "retry", "provider_server_error", 1),
    ],
)
def test_every_provider_answer_has_one_outcome_and_the_ledger_keeps_its_status(
    status: int, job_status: str, code: str, attempts: int
) -> None:
    keyed_system()
    job = CollectionJob.objects.get()
    assert not collect(job.pk, solcast(answer(status)), calculate, NOW)
    job.refresh_from_db()
    assert (job.status, job.last_error, job.attempts) == (job_status, code, attempts)
    assert (job.lease_token, job.lease_expires_at) == (None, None)
    ledger = RequestAttempt.objects.get(purpose="forecast")
    assert (ledger.outcome, ledger.http_status) == ("failed", status)


@pytest.mark.django_db(transaction=True)
def test_exhausted_provider_allowance_blocks_the_account_until_the_provider_day_resets() -> None:
    system = keyed_system()
    job = CollectionJob.objects.get()
    assert not collect(job.pk, solcast(answer(402)), calculate, NOW)
    job.refresh_from_db()
    system.account.refresh_from_db()
    assert job.available_at == PROVIDER_DAY_RESET
    assert system.account.request_blocked_until == PROVIDER_DAY_RESET
    with pytest.raises(RequestDeferredError):
        reserve_request(system.binding, NOW + timedelta(hours=1), purpose=RequestPurpose.FORECAST)
    assert RequestAttempt.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_unreachable_provider_and_unusable_answer_have_their_own_outcomes() -> None:
    keyed_system()
    job = CollectionJob.objects.get()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private address", request=request)

    assert not collect(job.pk, solcast(refuse), calculate, NOW)
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("retry", "provider_unreachable")
    assert RequestAttempt.objects.get(purpose="forecast").http_status is None
    unusable = solcast(lambda request: httpx.Response(200, json={"forecasts": "none"}))
    assert not collect(job.pk, unusable, calculate, job.available_at)
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("failed", "provider_invalid_response")


@pytest.mark.django_db(transaction=True)
def test_secret_echoing_answer_fails_once_without_a_second_request() -> None:
    keyed_system()
    job = CollectionJob.objects.get()
    provider = solcast(lambda request: httpx.Response(200, json={"error": "test-credential"}))
    assert not collect(job.pk, provider, calculate, NOW)
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("failed", "provider_invalid_response")
    assert not collect(job.pk, provider, calculate, NOW + timedelta(hours=1))
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 1


@pytest.mark.django_db(transaction=True)
def test_payload_postgresql_cannot_store_fails_once_without_a_second_request() -> None:
    keyed_system()
    job = CollectionJob.objects.get()
    body = {"forecasts": [], "note": "\u0000"}
    provider = solcast(lambda request: httpx.Response(200, json=body))
    assert not collect(job.pk, provider, calculate, NOW)
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("failed", "provider_invalid_response")
    assert not collect(job.pk, provider, calculate, NOW + timedelta(hours=1))
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 1
    assert not ForecastSnapshot.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("defect", ["secret", "site_id"])
def test_local_configuration_failure_spends_no_ledger_request(defect: str) -> None:
    system = keyed_system()
    if defect == "secret":
        ProviderAccount.objects.filter(pk=system.account.pk).update(
            encrypted_api_key="encrypted:v1:not-a-token"
        )
    else:
        SiteBinding.objects.filter(pk=system.binding.pk).update(site_id="../site")
    job = CollectionJob.objects.get()
    assert not collect(job.pk, solcast(unreachable), calculate, NOW)
    job.refresh_from_db()
    assert (job.status, job.last_error, job.attempts) == (
        "failed",
        "provider_configuration_invalid",
        1,
    )
    assert not RequestAttempt.objects.filter(purpose="forecast").exists()


@pytest.mark.django_db(transaction=True)
def test_database_failure_during_publication_schedules_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    def broken(*args: object, **kwargs: object) -> bool:
        raise DatabaseError("private detail")

    monkeypatch.setattr(jobs, "publish", broken)
    assert not collect(job.pk, FakeAdapter(), calculate, NOW)
    job.refresh_from_db()
    assert (job.status, job.last_error, job.attempts) == ("retry", "publication_failure", 1)
    assert (job.lease_token, job.lease_expires_at) == (None, None)
    monkeypatch.undo()
    assert collect(job.pk, FakeAdapter(), calculate, job.available_at)
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 1


@pytest.mark.django_db(transaction=True)
def test_lock_timeout_before_the_reservation_returns_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    def locked(*args: object, **kwargs: object) -> RequestAttempt:
        raise lock_timeout()

    monkeypatch.setattr(jobs, "reserve_request", locked)
    assert not collect(job.pk, FakeAdapter(), calculate, NOW)
    job.refresh_from_db()
    assert (job.status, job.last_error, job.attempts) == ("retry", "lock_contention", 0)
    assert not RequestAttempt.objects.filter(job=job).exists()


@pytest.mark.django_db(transaction=True)
def test_lock_timeout_after_a_paid_request_counts_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    def locked(*args: object, **kwargs: object) -> bool:
        raise lock_timeout()

    monkeypatch.setattr(jobs, "publish", locked)
    assert not collect(job.pk, FakeAdapter(), calculate, NOW)
    job.refresh_from_db()
    assert (job.status, job.last_error, job.attempts) == ("retry", "lock_contention", 1)


@pytest.mark.django_db(transaction=True)
def test_a_deterministic_failure_after_the_claim_ends_after_three_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    def broken(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("bug")

    monkeypatch.setattr(jobs, "publish", broken)
    for _ in range(3):
        assert not collect(job.pk, FakeAdapter(), calculate, job.available_at)
        job.refresh_from_db()
    assert (job.status, job.last_error, job.attempts) == ("failed", "publication_failure", 3)
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 1


@pytest.mark.django_db(transaction=True)
def test_delivery_of_a_missing_job_returns_quietly() -> None:
    runtime.deliver(10**9)


@pytest.mark.django_db(transaction=True)
def test_account_disabled_after_the_claim_records_account_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()
    original = jobs.claim

    def claim_then_disable(job_id: int, now: datetime) -> jobs.Lease | None:
        lease = original(job_id, now)
        ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=False)
        return lease

    monkeypatch.setattr(jobs, "claim", claim_then_disable)
    assert not collect(job.pk, FakeAdapter(), calculate, NOW)
    job.refresh_from_db()
    assert (job.status, job.last_error, job.attempts) == ("retry", "account_unavailable", 0)


@pytest.mark.django_db(transaction=True)
def test_superseded_slot_is_stale_before_the_attempt_limit_applies() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    CollectionJob.objects.filter(pk=job.pk).update(status="retry", attempts=3)
    assert claim(job.pk, NOW + timedelta(days=1)) is None
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("stale", "forecast_superseded")


@pytest.mark.django_db(transaction=True)
def test_revived_slot_job_carries_no_previous_outcome() -> None:
    from pyracast.forecasts.scheduler import enqueue_due

    make_system(approved=True)
    job = CollectionJob.objects.get()
    CollectionJob.objects.filter(pk=job.pk).update(status="stale", last_error="schedule_changed")
    assert job.pk in enqueue_due(NOW)
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("pending", "")


@pytest.mark.django_db(transaction=True)
def test_dispatch_publishes_the_hundred_oldest_due_jobs() -> None:
    system = make_system(approved=True)
    CollectionJob.objects.all().delete()
    created = CollectionJob.objects.bulk_create(
        CollectionJob(
            binding=system.binding,
            scheduled_for=NOW - timedelta(days=index),
            target_date=(NOW - timedelta(days=index)).date(),
            available_at=NOW - timedelta(minutes=index),
        )
        for index in range(101)
    )
    oldest = sorted(created[1:], key=lambda job: (job.available_at, job.pk))
    assert pending_job_ids(NOW) == [job.pk for job in oldest]


@pytest.mark.django_db(transaction=True)
def test_three_rate_limited_answers_end_a_forecast_job() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()

    def throttled() -> None:
        raise ProviderError("private", status_code=429, retry_after_seconds=1)

    moment = NOW
    for _ in range(3):
        assert not collect(job.pk, FakeAdapter(throttled), calculate, moment)
        job.refresh_from_db()
        system.account.refresh_from_db()
        moment = max(job.available_at, system.account.request_blocked_until or moment)
    assert (job.status, job.last_error, job.attempts) == ("failed", "provider_throttled", 3)
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 3


def dripping(chunks: int, size: int) -> Iterator[bytes]:
    yield b'{"forecasts": [], "pad": "'
    for _ in range(chunks):
        yield b"x" * size
    yield b'"}'


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks: Iterator[bytes]) -> None:
        self.chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self.chunks


def test_oversized_provider_answer_is_rejected_without_reading_it_all() -> None:
    read: list[int] = []

    def counted() -> Iterator[bytes]:
        for chunk in dripping(64, 64 * 1024):
            read.append(len(chunk))
            yield chunk

    provider = solcast(lambda request: httpx.Response(200, stream=Stream(counted())))
    with pytest.raises(ProviderError) as caught:
        provider.fetch(binding_for(), TARGET)
    assert caught.value.code == "provider_response_too_large"
    assert caught.value.retryable is False
    assert sum(read) < 3 * 1024 * 1024


def test_slow_provider_answer_ends_at_the_read_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter(range(0, 10_000, 20))
    monkeypatch.setattr("time.monotonic", lambda: next(ticks))
    provider = solcast(lambda request: httpx.Response(200, stream=Stream(dripping(10, 10))))
    with pytest.raises(ProviderError) as caught:
        provider.fetch(binding_for(), TARGET)
    assert caught.value.code == "provider_request_failed"
    assert caught.value.retryable is True


def test_oversized_weather_answer_is_rejected() -> None:
    weather = OpenMeteoProvider(
        httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Stream(dripping(64, 64 * 1024)))
        )
    )
    with pytest.raises(WeatherError) as caught:
        weather.fetch(40, 29, NOW, NOW + timedelta(hours=1))
    assert caught.value.code == "weather_request_failed"
