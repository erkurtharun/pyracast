from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from django.db import close_old_connections, connection

from pyracast.connections import operations
from pyracast.connections.contracts import ProviderData, ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts import jobs
from pyracast.forecasts.history import select_actual_snapshot
from pyracast.forecasts.jobs import claim, collect, pending_job_ids
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    ForecastSnapshot,
    RequestAttempt,
    RequestPurpose,
)
from pyracast.forecasts.schedule import configure_schedule
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.regions.models import Region
from tests.support.collection_schedule import HistoricalFixture, no_calculation
from tests.support.domain import NOW, make_system
from tests.support.jobs import calculate

pytestmark = pytest.mark.django_db(transaction=True)
NIGHT = (NOW + timedelta(days=1)).replace(hour=0)


def history_job() -> CollectionJob:
    enqueue_due(NIGHT)
    return CollectionJob.objects.get(
        endpoint="estimated_actuals",
        target_date=NIGHT.date() - timedelta(days=1),
        retry_until=NIGHT + timedelta(hours=6),
    )


@pytest.mark.parametrize(
    ("hour", "created"), [(-4, False), (-3, True), (0, True), (5, True), (6, False)]
)
def test_night_history_uses_local_window_independent_of_forecast_clocks(
    hour: int, created: bool
) -> None:
    system = make_system(approved=True)
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["16:00"],
        now=NOW,
    )
    now = NIGHT + timedelta(hours=hour)
    enqueue_due(now)
    assert CollectionJob.objects.filter(endpoint="estimated_actuals").exists() is created
    if created:
        job = CollectionJob.objects.get(endpoint="estimated_actuals")
        assert job.scheduled_for == NIGHT - timedelta(hours=3)
        assert job.retry_until == NIGHT + timedelta(hours=6)
        assert job.target_date == NIGHT.date() - timedelta(days=1)
        enqueue_due(now + timedelta(seconds=1))
        assert CollectionJob.objects.filter(endpoint="estimated_actuals").count() == 1
        assert claim(job.pk, now) is not None


def test_partial_history_is_immutable_available_and_retried_with_fresh_acquisition() -> None:
    system = make_system(approved=True)
    job = history_job()

    class Partial(HistoricalFixture):
        def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
            data = super().fetch_history(binding, target_date, days)
            return ProviderData(
                data.raw_payload,
                data.intervals[:-24] + data.intervals[-23:],
                collected_at=NIGHT,
                source_kind="estimated_actuals",
            )

    assert collect(job.pk, Partial(NIGHT), no_calculation, NIGHT)
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("succeeded", "history_incomplete")
    first = ForecastSnapshot.objects.get(job=job)
    assert (
        select_actual_snapshot(system.binding, job.target_date, NIGHT + timedelta(minutes=1))
        == first
    )
    followup = CollectionJob.objects.get(endpoint="estimated_actuals", status="pending")
    assert followup.available_at >= NIGHT + timedelta(minutes=30)
    assert followup.available_at < NIGHT + timedelta(minutes=31)
    assert not collect(job.pk, HistoricalFixture(NIGHT), no_calculation, NIGHT)
    assert collect(
        followup.pk, HistoricalFixture(followup.available_at), no_calculation, followup.available_at
    )
    first.refresh_from_db()
    assert len(first.normalized_intervals) == 335
    assert ForecastSnapshot.objects.count() == 2
    assert CollectionJob.objects.filter(endpoint="estimated_actuals").count() == 2
    assert RequestAttempt.objects.filter(purpose="estimated_actuals").count() == 2


@pytest.mark.parametrize("status", ["pending", "retry", "succeeded"])
def test_midnight_rollout_reuses_existing_three_am_history_slot(status: str) -> None:
    make_system(approved=True)
    job = history_job()
    CollectionJob.objects.filter(pk=job.pk).update(scheduled_for=NIGHT, status=status)
    if status == "succeeded":
        CollectionJob.objects.filter(pk=job.pk).update(status="pending")
        assert collect(job.pk, HistoricalFixture(NIGHT), no_calculation, NIGHT)
    enqueue_due(NIGHT + timedelta(minutes=1))
    assert CollectionJob.objects.filter(endpoint="estimated_actuals").count() == 1
    job.refresh_from_db()
    assert job.scheduled_for == NIGHT
    assert job.status == status


def test_history_transport_retries_half_hour_without_forecast_three_attempt_cap() -> None:
    make_system(approved=True)
    job = history_job()

    def unavailable() -> None:
        raise ProviderError("unavailable")

    moment = NIGHT
    for _ in range(4):
        assert not collect(job.pk, HistoricalFixture(moment, unavailable), no_calculation, moment)
        job.refresh_from_db()
        assert job.status == "retry"
        assert moment + timedelta(minutes=30) <= job.available_at < moment + timedelta(minutes=31)
        moment = job.available_at
    assert job.attempts == 4
    assert RequestAttempt.objects.filter(purpose="estimated_actuals").count() == 4
    assert collect(job.pk, HistoricalFixture(moment), no_calculation, moment)


def test_history_deadline_and_quota_are_terminal_without_dispatch_busy_loop() -> None:
    system = make_system(approved=True)
    job = history_job()
    assert claim(job.pk, NIGHT + timedelta(hours=6)) is None
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("failed", "history_window_closed")
    assert job.pk not in pending_job_ids(NIGHT + timedelta(hours=7))
    CollectionJob.objects.filter(pk=job.pk).update(status="pending")
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=1)
    assert not collect(job.pk, HistoricalFixture(NIGHT), no_calculation, NIGHT)
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("retry", "history_budget_reserved")
    assert not RequestAttempt.objects.filter(job=job).exists()
    assert job.pk not in pending_job_ids(NIGHT + timedelta(minutes=29))


def test_forecast_time_edit_preserves_history_and_global_disable_cancels_it() -> None:
    system = make_system(approved=True)
    job = history_job()
    configure_schedule(
        system.member, system.tenant.pk, system.region.pk, enabled=True, times=["18:00"], now=NIGHT
    )
    assert claim(job.pk, NIGHT) is not None
    CollectionJob.objects.filter(pk=job.pk).update(
        status="retry", lease_token=None, lease_expires_at=None
    )
    configure_schedule(
        system.member, system.tenant.pk, system.region.pk, enabled=False, times=[], now=NIGHT
    )
    job.refresh_from_db()
    assert job.status == "stale"
    assert claim(job.pk, NIGHT) is None


def test_concurrent_night_dispatch_and_redelivery_use_one_request() -> None:
    make_system(approved=True)
    barrier = Barrier(2)

    def dispatch() -> list[int]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return enqueue_due(NIGHT)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(dispatch) for _ in range(2)]
        assert results[0].result(timeout=15) == results[1].result(timeout=15)
    job = CollectionJob.objects.get(endpoint="estimated_actuals")

    def run() -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return collect(job.pk, HistoricalFixture(NIGHT), no_calculation, NIGHT)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(run) for _ in range(2)]
        assert sorted(result.result(timeout=15) for result in outcomes) == [False, True]
    assert RequestAttempt.objects.filter(job=job).count() == 1


@pytest.mark.parametrize("damage", ["empty", "gap", "missing", "negative", "overlap"])
def test_incomplete_history_schedules_another_immutable_request(damage: str) -> None:
    make_system(approved=True)
    job = history_job()

    class Damaged(HistoricalFixture):
        def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
            data = super().fetch_history(binding, target_date, days)
            intervals = list(data.intervals)
            if damage == "empty":
                intervals.clear()
            elif damage == "gap":
                del intervals[-24]
            elif damage == "overlap":
                intervals.append(intervals[-24])
            else:
                power = None if damage == "missing" else -1
                intervals[-24] = replace(intervals[-24], power_kw=power)
            return replace(data, intervals=tuple(intervals))

    assert collect(job.pk, Damaged(NIGHT), no_calculation, NIGHT)
    assert CollectionJob.objects.filter(endpoint="estimated_actuals", status="pending").count() == 1
    assert ForecastSnapshot.objects.get(job=job).available_at is not None


def test_final_partial_history_does_not_schedule_past_morning() -> None:
    make_system(approved=True)
    job = history_job()
    late = NIGHT + timedelta(hours=5, minutes=45)

    class Empty(HistoricalFixture):
        def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
            return ProviderData({}, (), collected_at=late, source_kind="estimated_actuals")

    assert collect(job.pk, Empty(late), no_calculation, late)
    assert CollectionJob.objects.filter(endpoint="estimated_actuals").count() == 1


def test_night_retry_budget_preserves_all_future_forecast_slots() -> None:
    system = make_system(approved=True)
    initial = CollectionJob.objects.get()
    assert collect(initial.pk, HistoricalFixture(NOW), calculate, NOW)
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["09:00", "16:00", "18:00"],
        now=NOW,
    )
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=4)
    yesterday_evening = NOW + timedelta(hours=2)
    enqueue_due(yesterday_evening)
    evening_job = CollectionJob.objects.get(endpoint="forecasts", scheduled_for=yesterday_evening)
    assert collect(
        evening_job.pk, HistoricalFixture(yesterday_evening), calculate, yesterday_evening
    )
    job = history_job()
    assert collect(job.pk, HistoricalFixture(NIGHT), no_calculation, NIGHT)
    extra = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="estimated_actuals",
        target_date=job.target_date,
        scheduled_for=NIGHT + timedelta(minutes=30),
        available_at=NIGHT + timedelta(minutes=30),
        retry_until=NIGHT + timedelta(hours=6),
    )
    assert collect(
        extra.pk, HistoricalFixture(extra.available_at), no_calculation, extra.available_at
    )
    assert RequestAttempt.objects.filter(purpose="estimated_actuals").count() == 1
    for hour in [6, 13, 15]:
        now = NIGHT.replace(hour=hour)
        enqueue_due(now)
        forecast = CollectionJob.objects.get(endpoint="forecasts", scheduled_for=now)
        assert collect(forecast.pk, HistoricalFixture(now), calculate, now)
    assert RequestAttempt.objects.filter(utc_day=NIGHT.date()).count() == 4


def test_crashed_night_worker_cannot_send_again_after_only_lease_expiry() -> None:
    make_system(approved=True)
    job = history_job()
    lease = claim(job.pk, NIGHT)
    assert lease is not None
    assert claim(job.pk, NIGHT + timedelta(minutes=6)) is None
    assert job.pk not in pending_job_ids(NIGHT + timedelta(minutes=6))
    assert claim(job.pk, NIGHT + timedelta(minutes=31)) is not None


def test_history_retries_after_utc_budget_reset_inside_local_night_window() -> None:
    system = make_system(approved=True)
    Region.objects.filter(pk=system.region.pk).update(timezone="Europe/Samara")
    CollectionJob.objects.update(status="succeeded")
    SiteBinding.objects.filter(pk=system.binding.pk).update(timezone="Europe/Samara")
    before_reset = NIGHT - timedelta(hours=1)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=2)
    RequestAttempt.objects.create(
        provider_account=system.account,
        utc_day=before_reset.date(),
        created_at=before_reset,
        purpose="validation",
        outcome="uncertain",
    )
    enqueue_due(before_reset)
    job = CollectionJob.objects.get(endpoint="estimated_actuals")
    assert not collect(job.pk, HistoricalFixture(before_reset), no_calculation, before_reset)
    job.refresh_from_db()
    assert (job.status, job.available_at) == ("retry", NIGHT)
    assert collect(job.pk, HistoricalFixture(NIGHT), no_calculation, NIGHT)


def test_shared_account_protects_slots_on_both_local_dates_within_utc_day() -> None:
    from pyracast.forecasts.budget import remaining_forecast_requests

    system = make_system(approved=True)
    initial = CollectionJob.objects.get()
    assert collect(initial.pk, HistoricalFixture(NOW), calculate, NOW)
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["01:00", "16:00"],
        now=NOW,
    )
    other = Region.objects.create(
        tenant=system.tenant,
        name="Other",
        latitude=41,
        longitude=29,
        timezone="Europe/Istanbul",
    )
    binding = SiteBinding.objects.create(
        region=other,
        provider_account=system.account,
        region_revision=1,
        site_id="other",
        capacity_kw=1,
        latitude=40,
        longitude=29,
        timezone=other.timezone,
        validated_at=NOW,
        validated_credential_revision=1,
    )
    other.status = "approved"
    other.approved_binding = binding
    other.save()
    CollectionJob.objects.create(
        binding=binding,
        endpoint="forecasts",
        scheduled_for=NOW,
        target_date=NOW.date() + timedelta(days=1),
        available_at=NOW,
        status="succeeded",
    )
    configure_schedule(
        system.member, system.tenant.pk, other.pk, enabled=True, times=["18:00"], now=NOW
    )
    assert remaining_forecast_requests(system.account, NIGHT) == 3


@pytest.mark.parametrize("boundary", ["account_lock", "before_http"])
def test_history_rechecks_morning_deadline_before_request(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    make_system(approved=True)
    job = history_job()
    late = NIGHT + timedelta(hours=6) - timedelta(seconds=1)
    if boundary == "account_lock":
        ticks = iter([0.0, 2.0])
        monkeypatch.setattr(operations, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    else:
        elapsed = [0.0]
        original_reserve = operations.reserve_request

        def reserve(
            binding: SiteBinding,
            now: datetime,
            *,
            purpose: RequestPurpose,
            job: CollectionJob | None = None,
        ) -> RequestAttempt:
            attempt = original_reserve(binding, now, purpose=purpose, job=job)
            elapsed[0] = 2.0
            return attempt

        monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
        monkeypatch.setattr(jobs, "reserve_request", reserve)

    def forbidden() -> None:
        pytest.fail("Morning deadline must prevent a new provider request")

    assert not collect(job.pk, HistoricalFixture(late, forbidden), no_calculation, late)
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("failed", "history_window_closed")
    assert RequestAttempt.objects.filter(job=job).count() == int(boundary == "before_http")


def test_unknown_history_transport_outcome_consumes_budget_and_waits_half_hour() -> None:
    make_system(approved=True)
    job = history_job()

    def interrupted() -> None:
        raise RuntimeError("Worker stopped after a possible outbound request")

    assert not collect(job.pk, HistoricalFixture(NIGHT, interrupted), no_calculation, NIGHT)
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("retry", "provider_unexpected_failure")
    assert RequestAttempt.objects.filter(job=job, outcome="uncertain").count() == 1
    assert not collect(
        job.pk, HistoricalFixture(NIGHT), no_calculation, NIGHT + timedelta(minutes=6)
    )
    resumed = job.available_at
    assert collect(job.pk, HistoricalFixture(resumed), no_calculation, resumed)
    assert RequestAttempt.objects.filter(job=job).count() == 2


@pytest.mark.parametrize("disable_during_request", [False, True])
def test_reenabling_collection_recovers_partial_history_retry_without_shortening_wait(
    disable_during_request: bool,
) -> None:
    system = make_system(approved=True)
    parent = history_job()

    class Empty(HistoricalFixture):
        def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
            if disable_during_request:
                configure_schedule(
                    system.member,
                    system.tenant.pk,
                    system.region.pk,
                    enabled=False,
                    times=[],
                    now=NIGHT,
                )
            return ProviderData({}, (), collected_at=NIGHT, source_kind="estimated_actuals")

    assert collect(parent.pk, Empty(NIGHT), no_calculation, NIGHT)
    child = CollectionJob.objects.get(endpoint="estimated_actuals", forecastsnapshot__isnull=True)
    original_available = child.available_at
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=False,
        times=[],
        now=NIGHT + timedelta(minutes=10),
    )
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["16:00"],
        now=NIGHT + timedelta(minutes=15),
    )
    enqueue_due(NIGHT + timedelta(minutes=15))
    child.refresh_from_db()
    assert (child.status, child.available_at) == ("pending", original_available)
    assert claim(child.pk, NIGHT + timedelta(minutes=15)) is None
    assert collect(
        child.pk, HistoricalFixture(original_available), no_calculation, original_available
    )
    parent.refresh_from_db()
    assert parent.last_error == "history_incomplete"


@pytest.mark.parametrize("inactive", ["stale_binding", "disabled_region", "unbootstrapped_region"])
def test_inactive_collection_does_not_block_shared_account_history_budget(inactive: str) -> None:
    system = make_system(approved=True)
    initial = CollectionJob.objects.get()
    assert collect(initial.pk, HistoricalFixture(NOW), calculate, NOW)
    job = history_job()
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=2)
    region = (
        system.region
        if inactive == "stale_binding"
        else Region.objects.create(
            tenant=system.tenant,
            name="Inactive collection",
            latitude=41,
            longitude=29,
            timezone="Europe/Istanbul",
        )
    )
    inactive_binding = SiteBinding.objects.create(
        region=region,
        provider_account=system.account,
        region_revision=2 if inactive == "stale_binding" else 1,
        site_id="inactive",
        capacity_kw=1,
        latitude=40,
        longitude=29,
        timezone=region.timezone,
        validated_at=NOW,
        validated_credential_revision=1,
    )
    if inactive != "stale_binding":
        region.status = "approved"
        region.approved_binding = inactive_binding
        region.save(update_fields=("status", "approved_binding"))
    if inactive != "unbootstrapped_region":
        CollectionJob.objects.create(
            binding=inactive_binding,
            endpoint="forecasts",
            scheduled_for=NIGHT + timedelta(hours=1),
            target_date=NIGHT.date() + timedelta(days=1),
            available_at=NIGHT,
        )
    if inactive == "disabled_region":
        CollectionSchedule.objects.create(
            region=region, enabled=False, times=["16:00"], effective_from=NOW
        )

    assert collect(job.pk, HistoricalFixture(NIGHT), no_calculation, NIGHT)
    assert RequestAttempt.objects.filter(job=job).count() == 1
    assert not RequestAttempt.objects.filter(binding=inactive_binding).exists()
