from datetime import timedelta

import pytest

from pyracast.connections.operations import rotate_secret, validate_binding
from pyracast.forecasts.jobs import claim, collect
from pyracast.forecasts.models import CollectionJob, CollectionSchedule, RequestAttempt
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.regions.models import Region
from pyracast.regions.operations import approve_region
from tests.test_domain import NOW, FakeAdapter, make_system
from tests.test_jobs import calculate

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("existing_stale_job", [False, True])
def test_revalidation_after_cutoff_recovers_tomorrows_job_without_duplicate_requests(
    existing_stale_job: bool,
) -> None:
    system = make_system(approved=True)
    initial = CollectionJob.objects.get()
    assert collect(initial.pk, FakeAdapter(), calculate, NOW)
    cutoff = NOW + timedelta(days=1)
    recovery_time = cutoff + timedelta(hours=1)
    existing = enqueue_due(cutoff) if existing_stale_job else []
    rotate_secret(system.admin, system.account.pk, "env:REVALIDATED_KEY")
    assert enqueue_due(cutoff) == []
    if existing:
        assert not collect(existing[0], FakeAdapter(), calculate, cutoff)
        assert CollectionJob.objects.get(pk=existing[0]).status == "stale"
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), recovery_time)
    approve_region(system.admin, system.region.pk, system.binding.pk, recovery_time)
    reserved_before_dispatch = RequestAttempt.objects.count()

    recovered = enqueue_due(recovery_time)

    assert len(recovered) == 1
    if existing:
        assert recovered == existing
    job = CollectionJob.objects.get(pk=recovered[0])
    assert job.status == "pending"
    assert job.scheduled_slot == cutoff
    assert job.target_date == cutoff.date() + timedelta(days=1)
    assert job.lease_token is None
    assert job.lease_expires_at is None
    assert RequestAttempt.objects.count() == reserved_before_dispatch
    assert enqueue_due(recovery_time + timedelta(minutes=15)) == recovered
    assert collect(job.pk, FakeAdapter(), calculate, recovery_time)
    assert not collect(job.pk, FakeAdapter(), calculate, recovery_time)
    assert RequestAttempt.objects.filter(job=job, purpose="forecast").count() == 1


def test_first_approval_after_cutoff_does_not_backfill_a_preapproval_slot() -> None:
    system = make_system()
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW - timedelta(hours=1))
    approval_time = NOW + timedelta(hours=1)
    initial = approve_region(system.admin, system.region.pk, system.binding.pk, approval_time)

    assert enqueue_due(approval_time) == []
    assert list(CollectionJob.objects.values_list("pk", flat=True)) == [initial.pk]
    assert initial.scheduled_slot == approval_time


def test_approval_acquisition_runs_outside_an_existing_schedule_without_enabling_other_slots() -> (
    None
):
    system = make_system()
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW - timedelta(hours=1))
    CollectionSchedule.objects.create(
        region=system.region,
        enabled=True,
        times=["09:15", "10:00"],
        effective_from=NOW - timedelta(hours=2),
    )
    initial = approve_region(system.admin, system.region.pk, system.binding.pk, NOW)

    assert collect(initial.pk, FakeAdapter(), calculate, NOW)
    assert RequestAttempt.objects.filter(job=initial).count() == 1
    later = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="forecasts",
        scheduled_slot=NOW + timedelta(minutes=1),
        available_at=NOW + timedelta(minutes=1),
        target_date=initial.target_date,
    )
    assert claim(later.pk, NOW + timedelta(minutes=1)) is None
    later.refresh_from_db()
    assert later.status == "stale"


def test_scheduler_requires_durable_approval_history() -> None:
    system = make_system()
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW - timedelta(hours=1))
    Region.objects.filter(pk=system.region.pk).update(
        status="approved", approved_binding=system.binding
    )

    assert enqueue_due(NOW) == []
    assert not CollectionJob.objects.exists()


def test_revalidation_does_not_reset_a_failed_jobs_retry_budget() -> None:
    system = make_system(approved=True)
    cutoff = NOW + timedelta(days=1)
    existing = enqueue_due(cutoff)
    CollectionJob.objects.filter(pk=existing[0]).update(
        status="failed", attempts=3, last_error="provider_failure"
    )
    rotate_secret(system.admin, system.account.pk, "env:REVALIDATED_KEY")
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), cutoff + timedelta(hours=1))

    assert enqueue_due(cutoff + timedelta(hours=1)) == existing
    job = CollectionJob.objects.get(pk=existing[0])
    assert (job.status, job.attempts, job.last_error) == ("failed", 3, "provider_failure")
