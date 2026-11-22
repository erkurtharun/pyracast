from datetime import date, datetime, timedelta

import httpx
import pytest

from pyracast.api.errors import exception_handler
from pyracast.connections.contracts import ProviderData, ProviderError
from pyracast.connections.models import ProviderAccount
from pyracast.epias import AUTH_URL, EpiasClient, EpiasDay, EpiasError, EpiasPlant
from pyracast.forecasts.jobs.claim import LEASE_DURATION, claim
from pyracast.forecasts.jobs.collect import persist_snapshot
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
    RequestPurpose,
)
from pyracast.forecasts.quota import (
    RequestDeferredError,
    record_provider_failure,
    reserve_account_request,
)
from pyracast.forecasts.schedule import configure_schedule
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.forecasts.timing import latest_forecast_slot
from pyracast.forecasts.validation import validate_binding
from pyracast.metering.jobs import run_epias_job
from pyracast.metering.models import EpiasCollectionJob
from pyracast.provider_retry import retry_delay
from pyracast.providers import SolcastProvider
from pyracast.weather_data import WeatherData
from tests.support.domain import NOW, FakeAdapter, make_system
from tests.support.jobs import calculate, collect, publish
from tests.support.metering import connected
from tests.support.providers import TARGET, binding_for, replace_key
from tests.support.weather_collection import WeatherFixture


@pytest.mark.parametrize(
    ("status", "retryable", "retry_after"),
    [
        (401, False, None),
        (403, False, None),
        (400, False, None),
        (429, True, 900),
        (500, True, None),
        (503, True, 900),
        (501, False, None),
        (505, False, None),
    ],
)
def test_solcast_preserves_safe_retry_policy(
    status: int, retryable: bool, retry_after: int | None
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="test-credential", headers={"Retry-After": "900"})

    with pytest.raises(ProviderError) as caught:
        SolcastProvider(httpx.MockTransport(handle)).fetch(binding_for(), TARGET)
    assert caught.value.retryable is retryable
    assert caught.value.retry_after_seconds == retry_after
    assert str(caught.value) == "provider_request_failed"


@pytest.mark.django_db(transaction=True)
def test_repeated_worker_interruptions_cannot_exceed_forecast_attempt_budget() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    moment = NOW
    for _ in range(3):
        lease = claim(job.pk, moment)
        assert lease is not None
        moment += LEASE_DURATION + timedelta(seconds=1)
    assert claim(job.pk, moment) is None
    job.refresh_from_db()
    assert (job.status, job.attempts, job.last_error) == ("failed", 3, "attempts_exhausted")
    assert job.lease_token is None
    assert job.lease_expires_at is None


@pytest.mark.django_db(transaction=True)
def test_quota_waits_do_not_consume_forecast_execution_attempts() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=1)
    assert RequestAttempt.objects.filter(
        provider_account=system.account, utc_day=NOW.date()
    ).exists()
    for _ in range(4):
        CollectionJob.objects.filter(pk=job.pk).update(available_at=NOW)
        assert not collect(job.pk, FakeAdapter(), calculate, NOW)
        job.refresh_from_db()
        assert (job.status, job.attempts) == ("retry", 0)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=10)
    CollectionJob.objects.filter(pk=job.pk).update(available_at=NOW)
    assert collect(job.pk, FakeAdapter(), calculate, NOW)
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 1


@pytest.mark.django_db(transaction=True)
def test_provider_retry_after_survives_collection_restart() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    def fail() -> None:
        raise ProviderError("private response", retry_after_seconds=900)

    assert not collect(job.pk, FakeAdapter(fail), calculate, NOW)
    job.refresh_from_db()
    assert NOW + timedelta(seconds=900) <= job.available_at < NOW + timedelta(seconds=931)
    assert not collect(job.pk, FakeAdapter(), calculate, NOW + timedelta(minutes=10))
    assert collect(job.pk, FakeAdapter(), calculate, job.available_at)


@pytest.mark.django_db(transaction=True)
def test_permanent_provider_failure_stops_automatic_retries() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    def fail() -> None:
        raise ProviderError("private response", retryable=False)

    assert not collect(job.pk, FakeAdapter(fail), calculate, NOW)
    job.refresh_from_db()
    assert (job.status, job.last_error, job.attempts) == ("failed", "provider_failure", 1)
    assert not collect(job.pk, FakeAdapter(), calculate, NOW + timedelta(hours=1))
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 1


@pytest.mark.django_db(transaction=True)
def test_rate_limit_cooldown_is_shared_by_account_and_does_not_consume_waiting_quota() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()

    def fail() -> None:
        raise ProviderError("private", status_code=429, retry_after_seconds=900)

    assert not collect(job.pk, FakeAdapter(fail), calculate, NOW)
    system.account.refresh_from_db()
    assert system.account.request_blocked_until is not None
    assert system.account.request_blocked_until >= NOW + timedelta(seconds=900)
    with pytest.raises(RequestDeferredError) as caught:
        reserve_account_request(
            system.account, NOW + timedelta(seconds=899), purpose=RequestPurpose.VALIDATION
        )
    assert caught.value.available_at == system.account.request_blocked_until
    CollectionJob.objects.filter(pk=job.pk).update(available_at=NOW)
    assert not collect(job.pk, FakeAdapter(), calculate, NOW)
    job.refresh_from_db()
    assert (job.status, job.attempts, job.last_error) == ("retry", 1, "provider_throttled")
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 1
    assert collect(job.pk, FakeAdapter(), calculate, job.available_at)


@pytest.mark.django_db(transaction=True)
def test_authentication_block_preserves_account_and_clears_by_credential_revision() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()
    record_provider_failure(system.account, ProviderError("private", status_code=401), NOW)
    assert not collect(job.pk, FakeAdapter(), calculate, NOW)
    job.refresh_from_db()
    assert (job.status, job.attempts, job.last_error) == (
        "failed",
        0,
        "provider_authentication_failed",
    )
    system.account.refresh_from_db()
    assert system.account.enabled
    with pytest.raises(RequestDeferredError) as caught:
        reserve_account_request(system.account, NOW, purpose=RequestPurpose.VALIDATION)
    assert caught.value.available_at is None
    replace_key(system.admin, system.account.pk, "test-only-replacement")
    system.account.refresh_from_db()
    assert reserve_account_request(system.account, NOW, purpose=RequestPurpose.VALIDATION)
    assert system.account.authentication_failed_revision != system.account.credential_revision
    assert not RequestAttempt.objects.filter(purpose="forecast").exists()


@pytest.mark.django_db(transaction=True)
def test_obsolete_responses_cannot_block_rotated_credentials_or_shorten_cooldown() -> None:
    system = make_system()
    original_account = ProviderAccount.objects.get(pk=system.account.pk)
    first_error = ProviderError("private", status_code=429, retry_after_seconds=3600)
    record_provider_failure(system.account, first_error, NOW)
    system.account.refresh_from_db()
    first_until = system.account.request_blocked_until
    record_provider_failure(system.account, ProviderError("private", status_code=429), NOW)
    system.account.refresh_from_db()
    assert system.account.request_blocked_until == first_until
    replace_key(system.admin, system.account.pk, "test-only-replacement")
    record_provider_failure(original_account, first_error, NOW)
    record_provider_failure(original_account, ProviderError("private", status_code=403), NOW)
    system.account.refresh_from_db()
    assert system.account.request_blocked_until == first_until
    assert system.account.authentication_failed_revision is None
    with pytest.raises(RequestDeferredError):
        reserve_account_request(system.account, NOW, purpose=RequestPurpose.VALIDATION)
    assert first_until is not None
    assert reserve_account_request(system.account, first_until, purpose=RequestPurpose.VALIDATION)


@pytest.mark.django_db(transaction=True)
def test_validation_authentication_error_blocks_other_requests() -> None:
    system = make_system()

    def fail() -> None:
        raise ProviderError("private", status_code=401, retryable=False)

    with pytest.raises(ProviderError):
        validate_binding(system.admin, system.binding.pk, FakeAdapter(fail), NOW)
    with pytest.raises(RequestDeferredError):
        reserve_account_request(system.account, NOW, purpose=RequestPurpose.INSPECT_SETTINGS)


@pytest.mark.parametrize("nightly", [False, True])
@pytest.mark.django_db(transaction=True)
def test_epias_permanent_authentication_failure_never_retries(
    monkeypatch: pytest.MonkeyPatch, nightly: bool
) -> None:
    connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None
    if nightly:
        job.retry_until = NOW + timedelta(hours=6)
        job.save(update_fields=("retry_until",))

    def fail(plant: EpiasPlant, day: date) -> EpiasDay:
        raise EpiasError("private", retryable=False)

    assert not run_epias_job(job.pk, fetch=fail, now=NOW)
    job.refresh_from_db()
    assert (job.status, job.attempts, job.last_error) == ("failed", 1, "epias_provider_failure")


@pytest.mark.parametrize(("authentication", "expected"), [(True, False), (False, True)])
def test_epias_refreshes_an_expired_ticket_but_stops_invalid_login(
    authentication: bool, expected: bool
) -> None:
    credential = "test-only-password"

    def handle(request: httpx.Request) -> httpx.Response:
        if authentication or str(request.url) != AUTH_URL:
            return httpx.Response(401)
        return httpx.Response(201, text="TGT-valid-ticket")

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(EpiasError) as caught,
    ):
        EpiasClient(client, username="username", password=credential).list_plants()
    assert caught.value.retryable is expected


@pytest.mark.parametrize("available", [None, NOW + timedelta(seconds=900)])
def test_deferred_request_api_is_safe_and_provides_retry_after(
    monkeypatch: pytest.MonkeyPatch, available: datetime | None
) -> None:
    monkeypatch.setattr("pyracast.api.errors.timezone.now", lambda: NOW)
    response = exception_handler(RequestDeferredError(available), {})
    assert response is not None
    assert response.status_code == (503 if available is None else 429)
    if available is not None:
        assert response["Retry-After"] == "900"


@pytest.mark.parametrize("jitter", [0, 30])
def test_retry_jitter_is_bounded_and_preserves_provider_minimum(
    monkeypatch: pytest.MonkeyPatch, jitter: int
) -> None:
    monkeypatch.setattr("pyracast.provider_retry.secrets.randbelow", lambda _: jitter)
    assert retry_delay(1) == timedelta(seconds=60 + jitter)
    assert retry_delay(100) == timedelta(seconds=1920 + jitter)
    assert retry_delay(1, 900) == timedelta(seconds=900 + jitter)
    assert retry_delay(1, minimum_seconds=1800) == timedelta(seconds=1800 + jitter)


@pytest.mark.django_db(transaction=True)
def test_unexpected_provider_errors_are_bounded_private_and_conservatively_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    def fail() -> None:
        raise RuntimeError("private secret from library")

    for _ in range(3):
        assert not collect(job.pk, FakeAdapter(fail), calculate, job.available_at)
        job.refresh_from_db()
    assert (job.status, job.attempts, job.last_error) == (
        "failed",
        3,
        "provider_unexpected_failure",
    )
    assert RequestAttempt.objects.filter(purpose="forecast", outcome="uncertain").count() == 3
    assert "private secret" not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.django_db(transaction=True)
def test_unexpected_auxiliary_weather_failure_does_not_block_primary_publication(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    make_system(approved=True)
    weather = WeatherFixture()

    def fail(latitude: float, longitude: float, start: datetime, end: datetime) -> WeatherData:
        raise RuntimeError("private weather key")

    monkeypatch.setattr(weather, "fetch", fail)
    assert collect(
        CollectionJob.objects.get().pk, FakeAdapter(), calculate, NOW, weather_provider=weather
    )
    assert ForecastResult.objects.count() == 1
    assert ForecastSnapshot.objects.get().weather_error == "weather_acquisition_failed"
    assert "private weather" not in caplog.text


@pytest.mark.django_db(transaction=True)
def test_resource_forbidden_does_not_disable_other_account_resources() -> None:
    system = make_system()
    record_provider_failure(system.account, ProviderError("private", status_code=403), NOW)
    assert reserve_account_request(system.account, NOW, purpose=RequestPurpose.VALIDATION)


@pytest.mark.django_db(transaction=True)
def test_restart_collapses_missed_forecast_slots_to_latest_due_slot() -> None:
    system = make_system(approved=True)
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["16:00", "17:00", "18:00"],
        now=NOW,
    )
    first_job = CollectionJob.objects.get()
    recovered_at = NOW + timedelta(hours=23)
    identifiers = enqueue_due(recovered_at)
    forecasts = CollectionJob.objects.filter(pk__in=identifiers, endpoint="forecasts")
    assert forecasts.count() == 1
    latest = forecasts.get()
    assert latest.scheduled_for == NOW + timedelta(hours=2)
    assert latest.available_at == recovered_at
    first_job.refresh_from_db()
    assert (first_job.status, first_job.last_error) == ("stale", "forecast_superseded")
    assert collect(latest.pk, FakeAdapter(), calculate, recovered_at)
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 1


@pytest.mark.django_db(transaction=True)
def test_claim_supersedes_old_retry_before_dispatch_creates_new_slot() -> None:
    system = make_system(approved=True)
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["16:00", "17:00"],
        now=NOW,
    )
    job = CollectionJob.objects.get()
    CollectionJob.objects.filter(pk=job.pk).update(status="retry")
    assert claim(job.pk, NOW + timedelta(hours=1)) is None
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("stale", "forecast_superseded")
    assert not RequestAttempt.objects.filter(purpose="forecast").exists()
    assert CollectionJob.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_new_slot_preserves_in_flight_response_and_completed_history() -> None:
    system = make_system(approved=True)
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["16:00", "16:01"],
        now=NOW,
    )
    original = CollectionJob.objects.get()
    lease = claim(original.pk, NOW)
    assert lease is not None
    acquired_at = NOW + timedelta(minutes=1)
    identifiers = enqueue_due(acquired_at)
    original.refresh_from_db()
    assert original.status == "running"
    snapshot = persist_snapshot(lease, ProviderData({}, ()), acquired_at)
    assert snapshot is not None
    assert publish(lease, snapshot, {}, acquired_at)
    assert identifiers != [original.pk]
    enqueue_due(acquired_at + timedelta(seconds=1))
    original.refresh_from_db()
    assert original.status == "succeeded"


@pytest.mark.parametrize(
    ("moment", "zone", "times", "expected"),
    [
        (
            datetime.fromisoformat("2026-03-29T02:00:00+00:00"),
            "Europe/Berlin",
            ["02:30"],
            datetime.fromisoformat("2026-03-28T01:30:00+00:00"),
        ),
        (
            datetime.fromisoformat("2026-10-25T02:00:00+00:00"),
            "Europe/Berlin",
            ["02:30"],
            datetime.fromisoformat("2026-10-25T00:30:00+00:00"),
        ),
        (NOW, "UTC", [], None),
    ],
)
def test_latest_forecast_slot_preserves_dst_policy(
    moment: datetime, zone: str, times: list[str], expected: datetime | None
) -> None:
    assert latest_forecast_slot(moment, zone, times, moment - timedelta(days=2)) == expected
