from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from typing import cast

import httpx
import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from rest_framework.test import APIClient

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.contracts import ProviderData, ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.jobs import claim, collect, persist_snapshot, retry
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.forecasts.schedule import configure_schedule, get_schedule, schedule_payload
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.providers import SolcastProvider
from pyracast.regions.models import AuditEvent, Region
from tests.support.collection_schedule import HistoricalFixture, no_calculation
from tests.support.domain import NOW, FakeAdapter, make_system
from tests.support.jobs import calculate
from tests.support.providers import binding_for

pytestmark = pytest.mark.django_db(transaction=True)
NIGHT = NOW.replace(hour=0)


def test_daily_actuals_request_archives_seven_completed_days_per_immutable_vintage() -> None:
    system = make_system(approved=True)
    initial_forecast = CollectionJob.objects.get()
    enqueue_due(NIGHT)
    history = CollectionJob.objects.get(endpoint="estimated_actuals")
    provider = HistoricalFixture(NIGHT)
    assert collect(history.pk, provider, no_calculation, NIGHT)
    assert not collect(history.pk, provider, no_calculation, NIGHT)
    initial = ForecastSnapshot.objects.get(job=history)
    assert initial.source_kind == "estimated_actuals"
    assert len(initial.normalized_intervals) == 336
    assert initial.coverage_start == datetime(2026, 11, 1, 21, tzinfo=UTC)
    assert initial.coverage_end == datetime(2026, 11, 8, 21, tzinfo=UTC)
    assert initial.available_at is not None
    assert initial.available_at >= NIGHT
    assert not ForecastResult.objects.exists()
    assert RequestAttempt.objects.filter(purpose="estimated_actuals").count() == 1
    assert collect(initial_forecast.pk, FakeAdapter(), calculate, NOW)
    assert ForecastResult.objects.count() == 1
    enqueue_due(NIGHT + timedelta(days=1))
    tomorrow = CollectionJob.objects.get(
        endpoint="estimated_actuals", target_date=date(2026, 11, 9)
    )
    provider.now += timedelta(days=1)
    assert collect(tomorrow.pk, provider, no_calculation, provider.now)
    latest = ForecastSnapshot.objects.get(job=tomorrow)
    assert len(latest.normalized_intervals) == 336
    assert provider.days == [7, 7]
    initial.refresh_from_db()
    assert len(initial.normalized_intervals) == 336
    assert initial.raw_payload["synthetic"] is True
    assert latest.job.binding_id == system.binding.pk


def test_failed_initial_history_is_backfilled_again_next_day() -> None:
    make_system(approved=True)
    enqueue_due(NIGHT)
    first = CollectionJob.objects.get(endpoint="estimated_actuals")

    def unavailable() -> None:
        raise ProviderError("private provider message")

    assert not collect(first.pk, HistoricalFixture(action=unavailable), no_calculation, NIGHT)
    enqueue_due(NIGHT + timedelta(days=1))
    next_job = CollectionJob.objects.get(
        endpoint="estimated_actuals", target_date=date(2026, 11, 9)
    )
    provider = HistoricalFixture(NIGHT + timedelta(days=1))
    assert collect(next_job.pk, provider, no_calculation, provider.now)
    assert provider.days == [7]
    assert RequestAttempt.objects.filter(purpose="estimated_actuals", outcome="failed").count() == 1
    assert len(ForecastSnapshot.objects.get().normalized_intervals) == 336


@pytest.mark.parametrize("endpoint", ["forecasts", "estimated_actuals"])
def test_successful_retry_clears_current_error_and_preserves_request_history(endpoint: str) -> None:
    make_system(approved=True)
    enqueue_due(NIGHT)
    moment = NIGHT if endpoint == "estimated_actuals" else NOW
    job = CollectionJob.objects.get(endpoint=endpoint)

    def unavailable() -> None:
        raise ProviderError("private provider message")

    failing = (
        HistoricalFixture(action=unavailable)
        if endpoint == "estimated_actuals"
        else FakeAdapter(unavailable)
    )
    assert not collect(job.pk, failing, calculate, moment)
    job.refresh_from_db()
    assert job.status == "retry"
    assert job.last_error == "provider_failure"

    assert collect(job.pk, HistoricalFixture(job.available_at), calculate, job.available_at)
    job.refresh_from_db()
    assert job.status == "succeeded"
    assert job.last_error == ""
    assert job.attempts == 2
    assert list(
        RequestAttempt.objects.filter(job=job)
        .order_by("created_at", "pk")
        .values_list("outcome", flat=True)
    ) == ["failed", "succeeded"]
    assert ForecastSnapshot.objects.get(job=job).available_at is not None


def test_history_restart_reuses_saved_snapshot_without_request() -> None:
    make_system(approved=True)
    enqueue_due(NIGHT)
    history = CollectionJob.objects.get(endpoint="estimated_actuals")
    lease = claim(history.pk, NIGHT)
    assert lease is not None
    data = HistoricalFixture().fetch_history(lease.binding, lease.target_date, 7)
    snapshot = persist_snapshot(lease, data, NIGHT)
    assert snapshot is not None
    retry(lease, NIGHT, "interrupted")
    history.refresh_from_db()
    assert collect(history.pk, FakeAdapter(), no_calculation, history.available_at)
    assert not RequestAttempt.objects.filter(job=history).exists()
    snapshot.refresh_from_db()
    assert snapshot.available_at is not None
    assert not ForecastResult.objects.exists()


def test_unsupported_and_mismatched_history_are_bounded_failures() -> None:
    make_system(approved=True)
    enqueue_due(NIGHT)
    history = CollectionJob.objects.get(endpoint="estimated_actuals")
    assert not collect(history.pk, FakeAdapter(), no_calculation, NIGHT)
    history.refresh_from_db()
    assert history.last_error == "provider_failure"

    class Mismatched(HistoricalFixture):
        def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
            return ProviderData({}, [], source_kind="forecast")

    assert not collect(history.pk, Mismatched(), no_calculation, history.available_at)
    history.refresh_from_db()
    assert history.last_error == "invalid_source_kind"
    assert not ForecastResult.objects.exists()


def test_daily_history_keeps_completed_seven_day_window_and_preserves_raw() -> None:
    system = make_system(approved=True)
    enqueue_due(NIGHT)
    initial = CollectionJob.objects.get(endpoint="estimated_actuals")
    assert collect(initial.pk, HistoricalFixture(), no_calculation, NIGHT)
    next_time = NIGHT + timedelta(days=1)
    enqueue_due(next_time)
    next_job = CollectionJob.objects.get(
        endpoint="estimated_actuals", target_date=date(2026, 11, 9)
    )

    class Rolling(HistoricalFixture):
        def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
            data = super().fetch_history(binding, target_date + timedelta(days=1), 9)
            return data

    assert collect(next_job.pk, Rolling(next_time), no_calculation, next_time)
    snapshot = ForecastSnapshot.objects.get(job=next_job)
    assert len(snapshot.normalized_intervals) == 336
    assert len(cast(list[object], snapshot.raw_payload["estimated_actuals"])) == 432
    assert snapshot.coverage_start == datetime(2026, 11, 2, 21, tzinfo=UTC)
    assert snapshot.coverage_end == datetime(2026, 11, 9, 21, tzinfo=UTC)
    assert snapshot.job.binding_id == system.binding.pk


@pytest.mark.parametrize(("days", "hours"), [(1, "13"), (7, "157")])
def test_solcast_actuals_requests_only_required_bounded_lookback(days: int, hours: str) -> None:

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rooftop_sites/site-1/estimated_actuals"
        assert dict(request.url.params) == {"format": "json", "hours": hours}
        assert request.headers["Authorization"] == "Bearer test-credential"
        return httpx.Response(200, json={"estimated_actuals": []})

    data = SolcastProvider(httpx.MockTransport(handle), clock=lambda: NOW).fetch_history(
        binding_for(), NOW.date(), days
    )
    assert data.source_kind == "estimated_actuals"
    assert data.issued_at is None


def test_schedule_configuration_quota_and_effective_boundary() -> None:
    system = make_system(approved=True)
    default = get_schedule(system.member, system.tenant.pk, system.region.pk, NOW)
    assert not CollectionSchedule.objects.exists()
    assert default.times == ["16:00"]
    payload = schedule_payload(default, NOW)
    assert payload["max_forecast_times"] == 9
    assert payload["history_local_time"] == "00:00"
    assert payload["history_retry_until_local"] == "09:00"
    assert payload["history_retry_minutes"] == 30
    configured = configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["18:00", "09:00"],
        now=NOW,
    )
    assert configured.times == ["09:00", "18:00"]
    assert get_schedule(system.member, system.tenant.pk, system.region.pk, NOW).pk == configured.pk
    assert enqueue_due(NOW) == []
    ids = enqueue_due(NOW + timedelta(hours=2))
    assert len(ids) == 2
    assert CollectionJob.objects.get(pk=ids[0]).endpoint == "forecasts"
    assert CollectionJob.objects.filter(endpoint="estimated_actuals").count() == 1
    tomorrow = enqueue_due(NOW + timedelta(days=1))
    assert len(tomorrow) == 1
    assert CollectionJob.objects.filter(endpoint="estimated_actuals").count() == 1
    configure_schedule(
        system.member, system.tenant.pk, system.region.pk, enabled=False, times=[], now=NOW
    )
    assert enqueue_due(NOW + timedelta(days=2)) == []
    assert claim(ids[0], NOW + timedelta(hours=2)) is None


@pytest.mark.parametrize("status", ["pending", "retry"])
def test_saving_unchanged_schedule_preserves_due_jobs_and_effective_boundary(status: str) -> None:
    system = make_system(approved=True)
    schedule = configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["17:00", "16:00"],
        now=NOW,
    )
    enqueue_due(NOW)
    jobs = CollectionJob.objects.filter(status="pending")
    job_ids = list(jobs.values_list("pk", flat=True))
    jobs.update(status=status, attempts=1, last_error="provider_failure")
    audit_count = AuditEvent.objects.filter(action="collection_schedule_updated").count()
    later = NOW + timedelta(minutes=2)

    saved = configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["16:00", "17:00"],
        now=later,
    )

    assert saved.pk == schedule.pk
    assert saved.effective_from == NOW
    assert AuditEvent.objects.filter(action="collection_schedule_updated").count() == audit_count
    assert CollectionJob.objects.filter(pk__in=job_ids, status=status, attempts=1).count() == 2
    assert all(claim(job_id, later) is not None for job_id in job_ids)


def test_three_daily_slots_create_forecasts_and_paired_history_jobs() -> None:
    system = make_system(approved=True)
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["09:00", "10:00", "11:00"],
        now=NOW,
    )
    assert enqueue_due(NOW) == []
    morning = (NOW + timedelta(days=1)).replace(hour=6)
    night = morning.replace(hour=0)
    enqueue_due(night)
    history = CollectionJob.objects.get(endpoint="estimated_actuals")
    assert collect(history.pk, HistoricalFixture(night), no_calculation, night)
    for hour in range(3):
        slot = morning + timedelta(hours=hour)
        enqueue_due(slot)
        forecast = CollectionJob.objects.get(endpoint="forecasts", scheduled_slot=slot)
        assert collect(forecast.pk, HistoricalFixture(slot), calculate, slot)
    assert ForecastResult.objects.count() == 3
    assert ForecastSnapshot.objects.filter(source_kind="forecast").count() == 3
    assert CollectionJob.objects.filter(endpoint="estimated_actuals").count() == 4
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 3
    assert RequestAttempt.objects.filter(purpose="estimated_actuals").count() == 1


@pytest.mark.parametrize("keep_due_time", [True, False])
def test_schedule_edit_keeps_authorized_retry_and_only_creates_future_slots(
    keep_due_time: bool,
) -> None:
    system = make_system(approved=True)
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["09:00", "10:00"],
        now=NOW,
    )
    due = (NOW + timedelta(days=1)).replace(hour=7)
    enqueue_due(due)
    job = CollectionJob.objects.get(endpoint="forecasts", scheduled_slot=due)
    lease = claim(job.pk, due)
    assert lease is not None
    retry(lease, due, "provider_failure")
    changed = due + timedelta(minutes=1)
    times = ["08:00", "09:00", "11:00"] + (["10:00"] if keep_due_time else [])
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=times,
        now=changed,
    )
    job.refresh_from_db()
    assert job.status == ("retry" if keep_due_time else "stale")
    assert job.attempts == 1
    assert job.last_error == "provider_failure"
    assert (claim(job.pk, due + timedelta(minutes=2)) is not None) is keep_due_time
    assert enqueue_due(changed) == []
    enqueue_due(due + timedelta(hours=1))
    assert (
        CollectionJob.objects.filter(
            endpoint="forecasts", scheduled_slot=due + timedelta(hours=1)
        ).count()
        == 1
    )
    assert not CollectionJob.objects.filter(scheduled_slot=due - timedelta(hours=2)).exists()


@pytest.mark.parametrize("times", [["16:00", "16:00"], ["1:00"], ["24:00"], ["16:00"] * 10])
def test_schedule_rejects_invalid_or_excess_times(times: list[str]) -> None:
    system = make_system(approved=True)
    with pytest.raises(ValidationError):
        configure_schedule(
            system.member, system.tenant.pk, system.region.pk, enabled=True, times=times, now=NOW
        )


def test_schedule_requires_approval_membership_and_aware_timestamp() -> None:
    system = make_system()
    with pytest.raises(ValidationError):
        configure_schedule(
            system.member,
            system.tenant.pk,
            system.region.pk,
            enabled=True,
            times=["09:00"],
            now=NOW,
        )
    with pytest.raises(ValidationError):
        configure_schedule(
            system.member,
            system.tenant.pk,
            system.region.pk,
            enabled=False,
            times=[],
            now=NOW.replace(tzinfo=None),
        )
    other = Tenant.objects.create(name="Other")
    with pytest.raises(PermissionDenied):
        get_schedule(system.member, other.pk, system.region.pk, NOW)
    with pytest.raises(PermissionDenied):
        configure_schedule(
            system.member, other.pk, system.region.pk, enabled=False, times=[], now=NOW
        )
    Membership.objects.create(user=system.member, tenant=other)
    with pytest.raises(Region.DoesNotExist):
        get_schedule(system.member, other.pk, system.region.pk, NOW)
    default = get_schedule(system.member, system.tenant.pk, system.region.pk, NOW)
    assert schedule_payload(default, NOW)["recent_jobs"] == []
    assert schedule_payload(default, NOW)["next_collection_at"] is None
    assert (
        configure_schedule(
            system.member, system.tenant.pk, system.region.pk, enabled=False, times=[], now=NOW
        ).enabled
        is False
    )


def test_disabled_or_removed_collection_time_cancels_unstarted_job() -> None:
    system = make_system(approved=True)
    initial = CollectionJob.objects.get()
    CollectionSchedule.objects.create(
        region=system.region, enabled=False, times=[], effective_from=NOW
    )
    assert claim(initial.pk, NOW) is None
    initial.refresh_from_db()
    assert initial.status == "stale"
    CollectionJob.objects.filter(pk=initial.pk).update(status="pending")
    CollectionSchedule.objects.filter(region=system.region).update(
        enabled=True, times=["17:00"], effective_from=NOW + timedelta(seconds=1)
    )
    assert claim(initial.pk, NOW) is None


def test_concurrent_daytime_schedulers_create_forecasts_without_history() -> None:
    system = make_system(approved=True)
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["16:00", "17:00"],
        now=NOW,
    )
    barrier = Barrier(2)

    def enqueue() -> list[int]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return enqueue_due(NOW + timedelta(hours=2))
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(enqueue), pool.submit(enqueue)]
        assert futures[0].result(timeout=15) == futures[1].result(timeout=15)
    assert CollectionJob.objects.filter(endpoint="forecasts").count() == 2
    assert CollectionJob.objects.filter(endpoint="estimated_actuals").count() == 0


def test_forecast_and_history_share_atomic_account_quota() -> None:
    system = make_system(approved=True)
    forecast = CollectionJob.objects.get()
    CollectionJob.objects.filter(pk=forecast.pk).update(available_at=NIGHT, scheduled_slot=NIGHT)
    enqueue_due(NIGHT)
    ids = list(CollectionJob.objects.values_list("pk", flat=True))
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=2)
    barrier = Barrier(2)

    def run(job_id: int) -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return collect(job_id, HistoricalFixture(), calculate, NIGHT)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, job_id) for job_id in ids]
        assert sorted(future.result(timeout=15) for future in futures) == [False, True]
    assert RequestAttempt.objects.count() == 2
    assert (
        CollectionJob.objects.filter(
            status__in=("failed", "retry"),
            last_error__in=("quota_exhausted", "history_budget_reserved"),
        ).count()
        == 1
    )


def test_schedule_api_reads_updates_and_scopes_tenant() -> None:
    system = make_system(approved=True)
    client = APIClient()
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/collection-schedule/"
    assert client.get(url).status_code == 401
    client.force_authenticate(system.member)
    response = client.get(url)
    assert response.status_code == 200
    assert response.json()["times"] == ["16:00"]
    response = client.put(url, {"enabled": True, "times": ["09:00", "16:00"]}, format="json")
    assert response.status_code == 200
    assert response.json()["history_requests_reserved_per_day"] == 1
    assert client.put(url, {"enabled": True, "times": ["25:00"]}, format="json").status_code == 400
    outsider = User.objects.create(username="outsider")
    client.force_authenticate(outsider)
    assert client.get(url).status_code == 403
    assert client.put(url, {"enabled": False, "times": []}, format="json").status_code == 403
    client.force_authenticate(system.member)
    assert client.put(url, {"enabled": True}, format="json").status_code == 400


def test_empty_initial_actuals_response_does_not_finish_backfill() -> None:
    make_system(approved=True)
    enqueue_due(NIGHT)
    first = CollectionJob.objects.get(endpoint="estimated_actuals")

    class Empty(HistoricalFixture):
        def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
            return ProviderData(
                {"estimated_actuals": []}, [], collected_at=NIGHT, source_kind="estimated_actuals"
            )

    assert collect(first.pk, Empty(), no_calculation, NIGHT)
    snapshot = ForecastSnapshot.objects.get(job=first)
    assert snapshot.coverage_start is None
    assert snapshot.coverage_end is None
    next_time = NIGHT + timedelta(days=1)
    enqueue_due(next_time)
    next_job = CollectionJob.objects.get(
        endpoint="estimated_actuals", target_date=date(2026, 11, 9)
    )
    provider = HistoricalFixture(next_time)
    assert collect(next_job.pk, provider, no_calculation, next_time)
    assert provider.days == [7]


def test_schedule_disable_during_request_preserves_authorized_acquisition() -> None:
    system = make_system(approved=True)
    initial = CollectionJob.objects.get()

    def disable() -> None:
        configure_schedule(
            system.member, system.tenant.pk, system.region.pk, enabled=False, times=[], now=NOW
        )

    assert collect(initial.pk, FakeAdapter(disable), calculate, NOW)
    initial.refresh_from_db()
    assert initial.status == "succeeded"
    assert ForecastSnapshot.objects.get(job=initial).available_at is not None
    assert ForecastResult.objects.filter(snapshot__job=initial).exists()
    assert enqueue_due(NOW + timedelta(days=1)) == []


def test_history_revalidation_rearms_stale_job_without_resetting_attempts() -> None:
    from pyracast.connections.operations import rotate_secret, validate_binding

    system = make_system(approved=True)
    enqueue_due(NIGHT)
    history = CollectionJob.objects.get(endpoint="estimated_actuals")
    rotate_secret(system.admin, system.account.pk, "test-only-revalidated-key")
    assert not collect(history.pk, HistoricalFixture(), no_calculation, NIGHT)
    history.refresh_from_db()
    assert history.status == "stale"
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NIGHT + timedelta(minutes=1))
    enqueue_due(NIGHT + timedelta(minutes=1))
    history.refresh_from_db()
    assert history.status == "pending"
    assert collect(history.pk, HistoricalFixture(), no_calculation, NIGHT + timedelta(minutes=1))
    assert RequestAttempt.objects.filter(job=history).count() == 1


def test_reconfigured_first_slot_reschedules_cancelled_unacquired_history() -> None:
    system = make_system(approved=True)
    enqueue_due(NIGHT)
    history = CollectionJob.objects.get(endpoint="estimated_actuals")
    changed = NIGHT + timedelta(minutes=15)
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["17:00"],
        now=changed,
    )
    history.refresh_from_db()
    assert history.status == "pending"
    enqueue_due(changed)
    new_slot = NIGHT + timedelta(hours=1)
    enqueue_due(new_slot)
    history.refresh_from_db()
    assert history.scheduled_slot == NIGHT - timedelta(hours=3)
    assert history.status == "pending"
    assert CollectionJob.objects.filter(endpoint="estimated_actuals").count() == 1
    assert collect(history.pk, HistoricalFixture(new_slot), no_calculation, new_slot)
    assert RequestAttempt.objects.filter(job=history).count() == 1


def test_collection_api_rejects_unknown_fields_and_accepts_account_bound_schedule() -> None:
    system = make_system(approved=True)
    client = APIClient()
    client.force_authenticate(system.member)
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/collection-schedule/"
    response = client.put(
        url, {"enabled": True, "times": ["16:00"], "tenant_id": system.tenant.pk}, format="json"
    )
    assert response.status_code == 400
    assert not CollectionSchedule.objects.exists()
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=12)
    times = [f"{hour:02}:00" for hour in range(10)]
    response = client.put(url, {"enabled": True, "times": times}, format="json")
    assert response.status_code == 200
    assert response.json()["times"] == times
    assert response.json()["max_forecast_times"] == 11
    assert (
        client.put(
            url, {"enabled": True, "times": [f"{hour:02}:00" for hour in range(12)]}, format="json"
        ).status_code
        == 400
    )


def test_reconfiguration_preserves_published_history_and_forecast_archives() -> None:
    system = make_system(approved=True)
    enqueue_due(NIGHT)
    history = CollectionJob.objects.get(endpoint="estimated_actuals")
    forecast = CollectionJob.objects.get(endpoint="forecasts")
    assert collect(history.pk, HistoricalFixture(), no_calculation, NIGHT)
    assert collect(forecast.pk, FakeAdapter(), calculate, NOW)
    history_snapshot = ForecastSnapshot.objects.get(job=history)
    original = (
        history_snapshot.raw_payload,
        history_snapshot.normalized_intervals,
        history_snapshot.available_at,
        history_snapshot.collected_at,
    )
    forecast_payload = ForecastResult.objects.get().payload
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["17:00"],
        now=NIGHT + timedelta(minutes=15),
    )
    enqueue_due(NIGHT + timedelta(hours=1))
    history.refresh_from_db()
    history_snapshot.refresh_from_db()
    assert history.scheduled_slot == NIGHT - timedelta(hours=3)
    assert history.status == "succeeded"
    assert (
        history_snapshot.raw_payload,
        history_snapshot.normalized_intervals,
        history_snapshot.available_at,
        history_snapshot.collected_at,
    ) == original
    assert ForecastResult.objects.get().payload == forecast_payload
    assert RequestAttempt.objects.filter(job=history).count() == 1
