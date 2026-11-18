from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier, Event
from typing import cast
from uuid import uuid4

import httpx
import pytest
from django.db import close_old_connections, connection

from pyracast.connections.secrets import encrypt_secret
from pyracast.forecasts.acquisition import missing_actuals_window
from pyracast.forecasts.jobs import Lease, claim, collect, fetch_history
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    ForecastSnapshot,
    HistoricalReferenceImport,
    RequestAttempt,
)
from pyracast.forecasts.schedule import configure_schedule
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.providers import FixtureProvider, SolcastProvider
from tests.support.domain import make_system
from tests.support.invariants import stored_again
from tests.support.providers import binding_for

MORNING = datetime(2026, 11, 15, 5, 45, tzinfo=UTC)
PERIOD = timedelta(minutes=30)


def raw_rows(start: datetime, end: datetime) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    while start < end:
        start += PERIOD
        rows.append({"period_end": start.isoformat(), "period": "PT30M", "pv_estimate": 1.0})
    return rows


@pytest.mark.parametrize(
    ("now", "timezone", "hours", "end"),
    [
        (MORNING, "Europe/Istanbul", "64", datetime(2026, 11, 17, 21, tzinfo=UTC)),
        (
            datetime(2026, 10, 31, 12, tzinfo=UTC),
            "America/New_York",
            "65",
            datetime(2026, 11, 3, 5, tzinfo=UTC),
        ),
        (
            datetime(2026, 3, 7, 13, tzinfo=UTC),
            "America/New_York",
            "63",
            datetime(2026, 3, 10, 4, tzinfo=UTC),
        ),
    ],
)
def test_forecast_requests_shortest_integer_hours_and_clips_three_local_dates(
    now: datetime, timezone: str, hours: str, end: datetime
) -> None:
    binding = binding_for()
    binding.timezone = timezone
    rows = raw_rows(now.replace(minute=0), now + timedelta(hours=80))

    def respond(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"format": "json", "hours": hours}
        return httpx.Response(200, json={"forecasts": rows})

    result = SolcastProvider(httpx.MockTransport(respond), clock=lambda: now).fetch(
        binding, now.date() + timedelta(days=1)
    )
    assert max(datetime.fromisoformat(cast(str, row["end"])) for row in result.intervals) == end
    assert all(datetime.fromisoformat(cast(str, row["start"])) < end for row in result.intervals)
    assert result.raw_payload == {"forecasts": rows}


def archive_actuals(binding_id: int, *, missing_after: datetime | None = None) -> ForecastSnapshot:
    start = (MORNING - timedelta(hours=168)).replace(minute=0)
    rows = raw_rows(start, MORNING.replace(minute=30))
    normalized = [
        {
            "start": (datetime.fromisoformat(cast(str, row["period_end"])) - PERIOD).isoformat(),
            "end": row["period_end"],
            "power_kw": row["pv_estimate"],
            "sunlight": "unknown",
        }
        for row in rows
    ]
    if missing_after is not None:
        normalized = [
            row
            for row in normalized
            if datetime.fromisoformat(cast(str, row["start"])) < missing_after
        ]
    job = CollectionJob.objects.create(
        binding_id=binding_id,
        endpoint="estimated_actuals",
        target_date=date(2026, 11, 14),
        scheduled_for=MORNING - timedelta(hours=1),
        available_at=MORNING - timedelta(hours=1),
        status="succeeded",
    )
    return ForecastSnapshot.objects.create(
        job=job,
        binding_id=binding_id,
        raw_payload={"estimated_actuals": rows},
        normalized_intervals=normalized,
        source_kind="estimated_actuals",
        collected_at=MORNING - timedelta(seconds=2),
        available_at=MORNING - timedelta(seconds=1),
        coverage_start=start,
        coverage_end=MORNING.replace(minute=30),
    )


def actual_job(binding_id: int) -> CollectionJob:
    return CollectionJob.objects.create(
        binding_id=binding_id,
        endpoint="estimated_actuals",
        scheduled_for=MORNING,
        target_date=MORNING.date(),
        available_at=MORNING,
        retry_until=MORNING + timedelta(minutes=15),
    )


@pytest.mark.django_db(transaction=True)
def test_archived_daylight_skips_actuals_api_despite_missing_nights() -> None:
    system = make_system(approved=True)
    snapshot = archive_actuals(system.binding.pk)
    snapshot.normalized_intervals = [
        row
        for row in snapshot.normalized_intervals
        if 4 <= datetime.fromisoformat(row["start"]).hour < 15
    ]
    stored_again(snapshot)
    assert missing_actuals_window(system.binding, MORNING) is None
    job = actual_job(system.binding.pk)

    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("Fully archived elapsed daylight must not consume provider quota")

    assert collect(job.pk, SolcastProvider(httpx.MockTransport(forbidden)), lambda *_: {}, MORNING)
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("succeeded", "history_already_archived")
    assert not RequestAttempt.objects.filter(job=job).exists()
    assert not ForecastSnapshot.objects.filter(job=job).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("missing_after", "hours"),
    [
        (datetime(2026, 11, 15, 4, 30, tzinfo=UTC), "2"),
        (datetime(2026, 11, 14, 4, 30, tzinfo=UTC), "26"),
        (None, "168"),
    ],
)
def test_actuals_requests_only_contiguous_lookback_needed_for_missing_daylight(
    missing_after: datetime | None, hours: str
) -> None:
    system = make_system(approved=True)
    if missing_after is not None:
        archive_actuals(system.binding.pk, missing_after=missing_after)
    system.account.encrypted_api_key = encrypt_secret("test-credential")
    system.account.save(update_fields=("encrypted_api_key",))
    job = actual_job(system.binding.pk)
    rows = raw_rows(MORNING - timedelta(hours=168, minutes=15), MORNING + PERIOD)

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/estimated_actuals")
        assert request.url.params["hours"] == hours
        return httpx.Response(200, json={"estimated_actuals": rows})

    provider = SolcastProvider(httpx.MockTransport(respond), clock=lambda: MORNING)
    assert collect(job.pk, provider, lambda *_: {}, MORNING)
    snapshot = ForecastSnapshot.objects.get(job=job)
    assert snapshot.raw_payload == {"estimated_actuals": rows}
    assert snapshot.coverage_end == MORNING.replace(minute=30)
    assert snapshot.coverage_start is not None
    assert snapshot.coverage_start >= MORNING - timedelta(hours=168)
    assert RequestAttempt.objects.filter(job=job).count() == 1


@pytest.mark.django_db(transaction=True)
def test_same_slot_pair_is_durable_and_concurrent_redelivery_uses_one_actuals_request() -> None:
    system = make_system(approved=True)
    system.account.encrypted_api_key = encrypt_secret("test-credential")
    system.account.save(update_fields=("encrypted_api_key",))
    CollectionSchedule.objects.create(
        region=system.region, enabled=True, times=["08:45"], effective_from=MORNING
    )
    barrier = Barrier(2)

    def dispatch() -> list[int]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return enqueue_due(MORNING)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: dispatch(), range(2)))
    assert results[0] == results[1]
    pair = CollectionJob.objects.get(endpoint="estimated_actuals", scheduled_for=MORNING)
    assert pair.target_date == date(2026, 11, 15)
    assert pair.retry_until == MORNING + timedelta(minutes=15)
    entered, release = Event(), Event()

    def respond(request: httpx.Request) -> httpx.Response:
        entered.set()
        assert release.wait(timeout=10)
        return httpx.Response(200, json={"estimated_actuals": []})

    def run() -> bool:
        close_old_connections()
        try:
            return collect(
                pair.pk,
                SolcastProvider(httpx.MockTransport(respond), clock=lambda: MORNING),
                lambda *_: {},
                MORNING,
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run)
        assert entered.wait(timeout=10)
        duplicate = pool.submit(run)
        assert duplicate.result(timeout=10) is False
        release.set()
        assert first.result(timeout=10)
    assert RequestAttempt.objects.filter(job=pair).count() == 1


@pytest.mark.django_db(transaction=True)
def test_paired_actuals_cannot_spend_reserved_future_forecast_quota() -> None:
    system = make_system(approved=True)
    CollectionJob.objects.update(status="succeeded")
    system.account.daily_quota = 3
    system.account.save(update_fields=("daily_quota",))
    CollectionSchedule.objects.create(
        region=system.region, enabled=True, times=["08:45", "16:00"], effective_from=MORNING
    )
    RequestAttempt.objects.create(
        provider_account=system.account,
        utc_day=MORNING.date(),
        created_at=MORNING,
        purpose="validation",
        outcome="succeeded",
    )
    RequestAttempt.objects.create(
        provider_account=system.account,
        utc_day=MORNING.date(),
        created_at=MORNING,
        purpose="validation",
        outcome="succeeded",
    )
    job = actual_job(system.binding.pk)

    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("Actuals cannot consume a reserved future forecast request")

    assert not collect(
        job.pk, SolcastProvider(httpx.MockTransport(forbidden)), lambda *_: {}, MORNING
    )
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("failed", "history_budget_reserved")
    assert job.attempts == 0
    assert not RequestAttempt.objects.filter(job=job).exists()


@pytest.mark.django_db(transaction=True)
def test_removing_forecast_clock_cancels_only_its_paired_actuals_job() -> None:
    system = make_system(approved=True)
    paired = actual_job(system.binding.pk)
    night = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="estimated_actuals",
        scheduled_for=MORNING.replace(hour=0, minute=0),
        target_date=MORNING.date() - timedelta(days=1),
        available_at=MORNING,
    )
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["16:00"],
        now=MORNING + timedelta(minutes=1),
    )
    paired.refresh_from_db()
    night.refresh_from_db()
    assert paired.status == "stale"
    assert night.status == "pending"


@pytest.mark.django_db(transaction=True)
def test_polar_night_does_not_require_archived_nighttime_values() -> None:
    system = make_system(approved=True)
    system.binding.latitude = 89
    system.binding.longitude = 0
    system.binding.timezone = "UTC"
    snapshot = archive_actuals(system.binding.pk)
    for row in snapshot.normalized_intervals:
        row["power_kw"] = 0
    stored_again(snapshot)
    assert missing_actuals_window(system.binding, MORNING) is None
    del snapshot.normalized_intervals[-1]
    stored_again(snapshot)
    assert missing_actuals_window(system.binding, MORNING) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("damage", ["null", "gap", "unfinished_native"])
def test_newer_actuals_coverage_masks_old_valid_rows_when_planning_refresh(damage: str) -> None:
    system = make_system(approved=True)
    archive_actuals(system.binding.pk)
    job = actual_job(system.binding.pk)
    CollectionJob.objects.filter(pk=job.pk).update(status="succeeded")
    start = datetime(2026, 11, 15, 4, 30, tzinfo=UTC)
    end = datetime(2026, 11, 15, 5, 30, tzinfo=UTC)
    first: dict[str, object] = {
        "start": start.isoformat(),
        "end": (start + PERIOD).isoformat(),
        "power_kw": 1.0,
    }
    if damage == "null":
        first["power_kw"] = None
    if damage == "unfinished_native":
        first["original_start"] = start.isoformat()
        first["original_end"] = datetime(2026, 11, 15, 6, tzinfo=UTC).isoformat()
    rows = [{"start": (start + PERIOD).isoformat(), "end": end.isoformat(), "power_kw": 1.0}]
    if damage != "gap":
        rows.insert(0, first)
    ForecastSnapshot.objects.create(
        job=job,
        binding=system.binding,
        source_kind="estimated_actuals",
        raw_payload={},
        normalized_intervals=rows,
        collected_at=MORNING - timedelta(seconds=1),
        available_at=MORNING - timedelta(microseconds=1),
        coverage_start=start,
        coverage_end=end,
    )
    assert missing_actuals_window(system.binding, MORNING) == (start, end)


@pytest.mark.django_db(transaction=True)
def test_imported_reference_cannot_suppress_native_actuals_collection() -> None:
    system = make_system(approved=True)
    snapshot = archive_actuals(system.binding.pk)
    reference = HistoricalReferenceImport.objects.create(
        binding=system.binding,
        content_sha256="a" * 64,
        source_name="authorized-export.json",
        source_settings={},
        imported_by=system.admin,
        created_at=MORNING,
        interval_count=1,
    )
    CollectionJob.objects.filter(pk=snapshot.job_id).update(
        endpoint="historical_import", reference_import=reference
    )
    assert missing_actuals_window(system.binding, MORNING) is not None


def test_sunrise_hour_requires_its_earlier_native_half_hour_for_hourly_meters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = binding_for("fixture")
    binding.latitude, binding.longitude, binding.timezone = 40, 29, "Europe/Istanbul"
    start = datetime(2026, 11, 15, 4, tzinfo=UTC)
    archive_start = (MORNING - timedelta(hours=168)).replace(minute=0)
    archive_end = MORNING.replace(minute=30)
    snapshot = ForecastSnapshot(
        pk=1,
        binding=binding,
        coverage_start=archive_start,
        coverage_end=archive_end,
        normalized_intervals=[
            {"start": (end - PERIOD).isoformat(), "end": end.isoformat(), "power_kw": 1.0}
            for row in raw_rows(archive_start, archive_end)
            if (end := datetime.fromisoformat(cast(str, row["period_end"]))) - PERIOD != start
        ],
    )
    monkeypatch.setattr(
        "pyracast.forecasts.acquisition.native_actual_snapshots", lambda *_: [snapshot]
    )
    assert missing_actuals_window(binding, MORNING) == (
        start,
        datetime(2026, 11, 15, 5, 30, tzinfo=UTC),
    )


def test_polar_day_requires_the_full_available_elapsed_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = binding_for("fixture")
    binding.latitude, binding.longitude = 89, 0
    noon = datetime(2026, 6, 21, 12, tzinfo=UTC)
    monkeypatch.setattr("pyracast.forecasts.acquisition.native_actual_snapshots", lambda *_: [])
    assert missing_actuals_window(binding, noon) == (noon - timedelta(hours=168), noon)


@pytest.mark.django_db(transaction=True)
def test_separate_actuals_jobs_cannot_hold_the_same_binding_lease_concurrently() -> None:
    system = make_system(approved=True)
    paired = actual_job(system.binding.pk)
    night = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="estimated_actuals",
        scheduled_for=MORNING - timedelta(minutes=30),
        target_date=MORNING.date() - timedelta(days=1),
        available_at=MORNING,
        retry_until=MORNING + timedelta(minutes=15),
    )
    barrier = Barrier(2)

    def acquire(job_id: int) -> Lease | None:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return claim(job_id, MORNING)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(acquire, job.pk) for job in (paired, night)]
        leases = [future.result(timeout=15) for future in futures]
    assert sum(lease is not None for lease in leases) == 1
    assert CollectionJob.objects.filter(pk__in=(paired.pk, night.pk), status="running").count() == 1
    assert (
        CollectionJob.objects.filter(
            pk__in=(paired.pk, night.pk), status="pending", attempts=0
        ).count()
        == 1
    )
    assert not RequestAttempt.objects.filter(job_id__in=(paired.pk, night.pk)).exists()


@pytest.mark.django_db(transaction=True)
def test_delayed_delivery_for_an_absent_job_consumes_no_request() -> None:
    assert claim(1, MORNING) is None
    assert not RequestAttempt.objects.exists()


def test_legacy_history_adapter_retains_seven_calendar_days_without_explicit_window() -> None:
    binding = binding_for("fixture")
    lease = Lease(1, binding, date(2026, 11, 14), uuid4(), "estimated_actuals")
    data = fetch_history(lease, FixtureProvider())
    assert data.source_kind == "estimated_actuals"
    assert len(data.intervals) == 336
    assert data.intervals[0]["start"] == "2026-11-08T00:00:00+00:00"
    assert data.intervals[-1]["end"] == "2026-11-15T00:00:00+00:00"
    assert data.raw_payload["synthetic"] is True


def test_midnight_proration_retains_native_interval_and_raw_payload() -> None:
    binding = binding_for()
    binding.timezone = "Asia/Kathmandu"
    rows = [{"period_end": "2026-11-17T18:30:00Z", "period": "PT30M", "pv_estimate": 1.0}]

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"forecasts": rows})

    data = SolcastProvider(httpx.MockTransport(respond), clock=lambda: MORNING).fetch(
        binding, date(2026, 11, 16)
    )
    assert data.intervals == [
        {
            "start": "2026-11-17T18:00:00+00:00",
            "end": "2026-11-17T18:15:00+00:00",
            "power_kw": 1.0,
            "sunlight": "unknown",
            "original_start": "2026-11-17T18:00:00+00:00",
            "original_end": "2026-11-17T18:30:00+00:00",
        }
    ]
    assert data.raw_payload == {"forecasts": rows}
