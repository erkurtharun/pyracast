import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.db import OperationalError, close_old_connections, connection, transaction

from pyracast.connections.contracts import ProviderData, ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.operations import QuotaExceededError, reserve_request, rotate_secret
from pyracast.forecasts import jobs
from pyracast.forecasts.jobs import (
    LEASE_DURATION,
    Lease,
    claim,
    collect,
    pending_job_ids,
    persist_snapshot,
    publish,
    retry,
)
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.regions.models import AuditEvent, Region
from pyracast.regions.operations import approve_region, transition_region
from tests.support.domain import NOW, FakeAdapter, make_system
from tests.support.jobs import calculate

pytestmark = pytest.mark.django_db(transaction=True)


def test_duplicate_delivery_publishes_one_immutable_vintage() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    assert collect(job.pk, FakeAdapter(), calculate, NOW)
    assert not collect(job.pk, FakeAdapter(), calculate, NOW)
    assert ForecastSnapshot.objects.count() == ForecastResult.objects.count() == 1
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 1
    assert not pending_job_ids(NOW + timedelta(days=1))
    result = ForecastResult.objects.get()
    assert result.payload["energy_kwh_m2"] is None
    assert result.snapshot.issued_at is None


def test_interrupted_lease_can_be_reclaimed_and_old_owner_cannot_publish() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    first = claim(job.pk, NOW)
    assert first is not None
    assert claim(job.pk, NOW) is None
    assert pending_job_ids(NOW + LEASE_DURATION + timedelta(seconds=1)) == [job.pk]
    second = claim(job.pk, NOW + LEASE_DURATION + timedelta(seconds=1))
    assert second is not None
    assert first.token != second.token
    assert (
        persist_snapshot(first, ProviderData({}, []), NOW + LEASE_DURATION + timedelta(seconds=1))
        is None
    )
    snapshot = persist_snapshot(
        second, ProviderData({"vintage": 2}, []), NOW + LEASE_DURATION + timedelta(seconds=1)
    )
    assert snapshot is not None
    assert not publish(first, snapshot, {}, NOW + LEASE_DURATION + timedelta(seconds=1))
    retry(first, NOW, "old_worker")
    assert publish(second, snapshot, {"valid": True}, NOW + LEASE_DURATION + timedelta(seconds=1))
    assert ForecastSnapshot.objects.get().raw_payload == {"vintage": 2}


def test_expired_lease_cannot_persist_or_publish_even_without_redelivery() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    lease = claim(job.pk, NOW)
    assert lease is not None
    snapshot = persist_snapshot(lease, ProviderData({}, []), NOW)
    assert snapshot is not None
    assert (
        persist_snapshot(lease, ProviderData({}, []), NOW + LEASE_DURATION + timedelta(seconds=1))
        is None
    )
    assert not publish(lease, snapshot, {}, NOW + LEASE_DURATION + timedelta(seconds=1))
    CollectionJob.objects.filter(pk=job.pk).update(lease_expires_at=None)
    assert persist_snapshot(lease, ProviderData({}, []), NOW) is None
    assert not publish(lease, snapshot, {}, NOW)


def test_snapshot_reused_after_calculation_failure_without_second_provider_request() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    def fail(binding: SiteBinding, target_date: object, data: ProviderData) -> dict[str, object]:
        raise ValueError("invalid numerical inputs")

    assert not collect(job.pk, FakeAdapter(), fail, NOW)
    assert ForecastSnapshot.objects.count() == 1
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("retry", "calculation_failure")
    assert claim(job.pk, NOW) is None
    assert collect(job.pk, FakeAdapter(), calculate, job.available_at)
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 1


def test_estimated_actuals_snapshot_cannot_be_published_after_restart() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    lease = claim(job.pk, NOW)
    assert lease is not None
    snapshot = persist_snapshot(lease, ProviderData({}, [], source_kind="estimated_actuals"), NOW)
    assert snapshot is not None
    snapshot.refresh_from_db()
    assert snapshot.source_kind == "estimated_actuals"
    with pytest.raises(ValidationError):
        publish(lease, snapshot, {}, NOW)
    retry(lease, NOW, "interrupted")
    job.refresh_from_db()
    assert not collect(job.pk, FakeAdapter(), calculate, job.available_at)
    job.refresh_from_db()
    assert job.last_error == "invalid_source_kind"
    assert not ForecastResult.objects.exists()
    assert not RequestAttempt.objects.filter(purpose="forecast").exists()


def test_collection_reserves_quota_after_time_spent_claiming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    clock = [0.0]
    original_claim = jobs.claim

    def delayed_claim(job_id: int, now: object) -> Lease | None:
        assert isinstance(now, type(NOW))
        lease = original_claim(job_id, now)
        clock[0] = 2.0
        return lease

    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(jobs, "claim", delayed_claim)
    before_midnight = NOW.replace(hour=23, minute=59, second=59)
    assert collect(job.pk, FakeAdapter(), calculate, before_midnight)
    assert (
        RequestAttempt.objects.get(purpose="forecast").utc_day
        == (before_midnight + timedelta(days=1)).date()
    )


def test_provider_retries_consume_quota_and_fail_after_bounded_attempts() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    def fail() -> None:
        raise ProviderError("sensitive exception must not be stored")

    for _ in range(3):
        assert not collect(job.pk, FakeAdapter(fail), calculate, job.available_at)
        job.refresh_from_db()
    assert (job.status, job.last_error, job.attempts) == ("failed", "provider_failure", 3)
    assert RequestAttempt.objects.filter(purpose="forecast", outcome="failed").count() == 3
    assert not ForecastSnapshot.objects.exists()
    assert claim(job.pk, job.available_at) is None


def test_quota_backoff_survives_until_next_utc_day() -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=1)
    job = CollectionJob.objects.get()
    assert not collect(job.pk, FakeAdapter(), calculate, NOW)
    job.refresh_from_db()
    assert job.status == "retry"
    assert job.available_at.isoformat() == "2026-11-10T00:00:00+00:00"
    assert job.last_error == "quota_exhausted"
    assert collect(job.pk, FakeAdapter(), calculate, job.available_at)


def test_quota_retry_after_midnight_waits_for_the_following_utc_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()
    before_midnight = NOW.replace(hour=23, minute=59, second=59)
    after_midnight = before_midnight + timedelta(seconds=2)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=1)
    reserve_request(system.binding, after_midnight, purpose="validation")
    clock = [0.0]
    original_claim = jobs.claim

    def delayed_claim(job_id: int, now: datetime) -> Lease | None:
        lease = original_claim(job_id, now)
        clock[0] = 2.0
        return lease

    def reject_request() -> None:
        pytest.fail("An exhausted UTC-day budget must prevent provider access")

    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(jobs, "claim", delayed_claim)

    assert not collect(job.pk, FakeAdapter(reject_request), calculate, before_midnight)

    job.refresh_from_db()
    assert job.status == "retry"
    assert job.available_at.isoformat() == "2026-11-11T00:00:00+00:00"
    assert pending_job_ids(after_midnight) == []
    assert not RequestAttempt.objects.filter(purpose="forecast").exists()


@pytest.mark.parametrize("failure", ["provider", "calculation"])
def test_failure_backoff_starts_after_the_failed_operation_finishes(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    clock = [0.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def fail_provider() -> None:
        if failure == "provider":
            clock[0] = 90.0
            raise ProviderError("Provider failed after a delayed response")

    def fail_calculation(
        binding: SiteBinding, target_date: object, data: ProviderData
    ) -> dict[str, object]:
        clock[0] = 90.0
        raise ValueError("Calculation failed after processing the snapshot")

    assert not collect(job.pk, FakeAdapter(fail_provider), fail_calculation, NOW)

    job.refresh_from_db()
    assert job.status == "retry"
    delay = 330 if failure == "provider" else 150
    assert NOW + timedelta(seconds=delay) <= job.available_at < NOW + timedelta(seconds=delay + 31)
    assert pending_job_ids(NOW + timedelta(seconds=90)) == []


@pytest.mark.parametrize("change", ["suspended", "revision", "credential", "disabled"])
def test_stale_binding_is_rejected_before_network(change: str) -> None:
    system = make_system(approved=True)
    if change == "suspended":
        transition_region(system.admin, system.region.pk, "suspended")
    elif change == "revision":
        Region.objects.filter(pk=system.region.pk).update(revision=2)
    elif change == "credential":
        rotate_secret(system.admin, system.account.pk, "test-only-new-key")
    else:
        ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=False)
    job = CollectionJob.objects.get()
    assert not collect(job.pk, FakeAdapter(), calculate, NOW)
    job.refresh_from_db()
    assert job.status == "stale"
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 0


def test_approval_change_during_network_prevents_publication_but_preserves_snapshot() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()

    def suspend() -> None:
        transition_region(system.admin, system.region.pk, "suspended")

    adapter = FakeAdapter(suspend)
    assert not collect(job.pk, adapter, calculate, NOW)
    assert ForecastSnapshot.objects.count() == 1
    assert ForecastResult.objects.count() == 0
    job.refresh_from_db()
    assert job.status == "stale"


def test_stale_worker_fetch_cannot_overwrite_reclaimed_lease() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    def steal() -> None:
        CollectionJob.objects.filter(pk=job.pk).update(lease_token=uuid.uuid4())

    assert not collect(job.pk, FakeAdapter(steal), calculate, NOW)
    assert not ForecastSnapshot.objects.exists()


def test_missing_running_lease_is_recoverable_and_naive_now_rejected() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()
    CollectionJob.objects.filter(pk=job.pk).update(status="running", lease_expires_at=None)
    lease = claim(job.pk, NOW)
    assert lease is not None
    with pytest.raises(ValidationError):
        claim(job.pk, NOW.replace(tzinfo=None))
    retry(lease, NOW, "retry")
    retry(lease, NOW, "duplicate")
    job.refresh_from_db()
    assert job.last_error == "retry"
    assert isinstance(lease, Lease)
    assert lease.binding.pk == system.binding.pk


def test_scheduler_deduplicates_local_cutoff_and_recovers_missed_slot() -> None:
    make_system(approved=True)
    jobs = enqueue_due(NOW)
    assert len(jobs) == 2
    assert jobs[:1] == enqueue_due(NOW + timedelta(hours=1))
    scheduled = CollectionJob.objects.get(pk=jobs[0])
    assert scheduled.scheduled_slot == NOW
    assert scheduled.target_date.isoformat() == "2026-11-10"
    assert enqueue_due(NOW + timedelta(hours=20)) == jobs[:1]
    next_day = enqueue_due(NOW + timedelta(days=1))
    assert next_day != jobs
    assert CollectionJob.objects.get(pk=next_day[0]).target_date.isoformat() == "2026-11-11"


def test_scheduler_ignores_unvalidated_or_not_yet_valid_slots() -> None:
    system = make_system()
    Region.objects.filter(pk=system.region.pk).update(approved_binding=system.binding)
    assert enqueue_due(NOW) == []
    Region.objects.filter(pk=system.region.pk).update(status="approved")
    assert enqueue_due(NOW) == []
    SiteBinding.objects.filter(pk=system.binding.pk).update(validated_at=NOW + timedelta(seconds=1))
    assert enqueue_due(NOW) == []
    with pytest.raises(ValidationError):
        enqueue_due(NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        enqueue_due(NOW, cutoff_hour=24)


def test_concurrent_quota_reservations_share_one_account_budget() -> None:
    system = make_system()
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=1)
    barrier = Barrier(2)

    def reserve() -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            reserve_request(system.binding, NOW, purpose="forecast")
            return True
        except QuotaExceededError:
            return False
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve), pool.submit(reserve)]
        assert sorted(future.result(timeout=15) for future in futures) == [False, True]
    assert RequestAttempt.objects.count() == 1


def test_concurrent_approvals_share_atomic_first_job() -> None:
    system = make_system(approved=True)
    CollectionJob.objects.all().delete()
    Region.objects.filter(pk=system.region.pk).update(status="pending", approved_binding=None)
    AuditEvent.objects.filter(action="region_approved").delete()
    barrier = Barrier(2)

    def approve() -> int:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return approve_region(system.admin, system.region.pk, system.binding.pk, NOW).pk
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve), pool.submit(approve)]
        assert len({future.result(timeout=15) for future in futures}) == 1
    assert CollectionJob.objects.count() == 1
    assert AuditEvent.objects.filter(action="region_approved").count() == 1


def test_concurrent_job_claims_have_a_single_lease_owner() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    barrier = Barrier(2)

    def acquire() -> Lease | None:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return claim(job.pk, NOW)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(acquire), pool.submit(acquire)]
        assert sum(future.result(timeout=15) is not None for future in futures) == 1


def test_publication_rejects_snapshot_from_another_job() -> None:
    system = make_system(approved=True)
    first = CollectionJob.objects.get()
    other = CollectionJob.objects.create(
        binding=system.binding,
        scheduled_slot=NOW + timedelta(days=1),
        target_date=first.target_date + timedelta(days=1),
        available_at=NOW,
    )
    first_lease = claim(first.pk, NOW)
    other_lease = claim(other.pk, NOW)
    assert first_lease is not None
    assert other_lease is not None
    snapshot = persist_snapshot(first_lease, ProviderData({}, []), NOW)
    assert snapshot is not None
    with pytest.raises(ValidationError):
        publish(other_lease, snapshot, {"wrong": True}, NOW)
    assert not ForecastResult.objects.exists()
    other.refresh_from_db()
    assert other.status == "running"


def test_publication_holds_account_lock_against_credential_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()
    lease = claim(job.pk, NOW)
    assert lease is not None
    snapshot = persist_snapshot(lease, ProviderData({}, []), NOW)
    assert snapshot is not None
    account_checked = Event()
    rotation_blocked = Event()
    original = jobs.binding_is_current

    def checked(binding: SiteBinding) -> bool:
        current = original(binding)
        account_checked.set()
        assert rotation_blocked.wait(timeout=10)
        return current

    monkeypatch.setattr(jobs, "binding_is_current", checked)

    def publish_result() -> bool:
        close_old_connections()
        try:
            return publish(lease, snapshot, {"valid": True}, NOW)
        finally:
            connection.close()

    def rotate() -> None:
        close_old_connections()
        try:
            assert account_checked.wait(timeout=10)
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '100ms'")
                with pytest.raises(OperationalError):
                    rotate_secret(system.admin, system.account.pk, "test-only-new-key")
        finally:
            rotation_blocked.set()
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        published = pool.submit(publish_result)
        rotated = pool.submit(rotate)
        rotated.result(timeout=15)
        assert published.result(timeout=15)
    system.account.refresh_from_db()
    assert system.account.credential_revision == 1


def test_lease_expiration_includes_time_waiting_for_publication_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    lease = claim(job.pk, NOW)
    assert lease is not None
    snapshot = persist_snapshot(lease, ProviderData({}, []), NOW)
    assert snapshot is not None
    ticks = iter([0.0, LEASE_DURATION.total_seconds() + 1])
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert not publish(lease, snapshot, {}, NOW)
    assert not ForecastResult.objects.exists()
