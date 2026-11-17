from datetime import timedelta

import pytest

from pyracast.connections.contracts import ProviderData, ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.operations import rotate_secret, validate_binding
from pyracast.forecasts.jobs import collect, pending_job_ids
from pyracast.forecasts.models import CollectionJob, CollectionSchedule, RequestAttempt
from pyracast.forecasts.timing import effective_forecast_schedule
from pyracast.regions.models import Region
from tests.support.domain import NOW, FakeAdapter, make_system
from tests.support.jobs import calculate


def test_effective_collection_schedule_covers_default_trim_disabled_and_approval_boundary() -> None:
    timing = effective_forecast_schedule(first_slot=NOW, schedule=None, daily_quota=10)
    assert timing is not None
    assert (timing.times, timing.effective_from) == (("16:00",), NOW)
    schedule = CollectionSchedule(
        enabled=True, times=["16:00", "17:00"], effective_from=NOW - timedelta(hours=1)
    )
    timing = effective_forecast_schedule(first_slot=NOW, schedule=schedule, daily_quota=2)
    assert timing is not None
    assert (timing.times, timing.effective_from) == (("16:00",), NOW)
    schedule.enabled = False
    assert effective_forecast_schedule(first_slot=NOW, schedule=schedule, daily_quota=10) is None
    assert effective_forecast_schedule(first_slot=None, schedule=None, daily_quota=10) is None


@pytest.mark.django_db(transaction=True)
def test_missing_rate_limit_header_defers_account_and_job_for_at_least_fifteen_minutes() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()

    def fail() -> None:
        raise ProviderError("private", status_code=429)

    assert not collect(job.pk, FakeAdapter(fail), calculate, NOW)
    job.refresh_from_db()
    system.account.refresh_from_db()
    assert job.last_error == "provider_throttled"
    assert job.available_at >= NOW + timedelta(minutes=15)
    assert system.account.request_blocked_until is not None
    assert system.account.request_blocked_until >= NOW + timedelta(minutes=15)


@pytest.mark.django_db(transaction=True)
def test_manual_cooldown_reset_wakes_only_the_previously_throttled_job() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()

    def fail() -> None:
        raise ProviderError("private", status_code=429, retry_after_seconds=3600)

    assert not collect(job.pk, FakeAdapter(fail), calculate, NOW)
    CollectionJob.objects.create(
        binding=system.binding,
        scheduled_slot=NOW + timedelta(seconds=2),
        target_date=job.target_date,
        available_at=NOW + timedelta(hours=2),
        status="retry",
        last_error="provider_failure",
    )
    assert pending_job_ids(NOW) == []
    assert not collect(job.pk, FakeAdapter(), calculate, NOW + timedelta(seconds=1))
    ProviderAccount.objects.filter(pk=system.account.pk).update(request_blocked_until=None)
    assert pending_job_ids(NOW) == [job.pk]
    assert collect(job.pk, FakeAdapter(), calculate, NOW + timedelta(seconds=1))
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 2


@pytest.mark.django_db(transaction=True)
def test_repaired_credentials_rearm_the_latest_authentication_failure() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()

    def fail() -> None:
        raise ProviderError("private", status_code=401, retryable=False)

    assert not collect(job.pk, FakeAdapter(fail), calculate, NOW)
    job.refresh_from_db()
    assert job.last_error == "provider_authentication_failed"
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 1
    rotate_secret(system.admin, system.account.pk, "test-only-new-credentials")
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW + timedelta(seconds=1))
    job.refresh_from_db()
    assert (job.status, job.attempts, job.last_error) == ("pending", 0, "")
    assert collect(job.pk, FakeAdapter(), calculate, NOW + timedelta(seconds=2))
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 2


@pytest.mark.parametrize(
    ("status", "error"),
    [("failed", "provider_failure"), ("pending", "provider_authentication_failed")],
)
@pytest.mark.django_db(transaction=True)
def test_successful_validation_preserves_jobs_without_a_terminal_auth_failure(
    status: str, error: str
) -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()
    CollectionJob.objects.filter(pk=job.pk).update(status=status, last_error=error, attempts=1)
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    job.refresh_from_db()
    assert (job.status, job.last_error, job.attempts) == (status, error, 1)


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.django_db(transaction=True)
def test_validation_cannot_revive_collection_for_a_suspended_region_or_missing_job(
    approved: bool,
) -> None:
    system = make_system(approved=True)
    if approved:
        CollectionJob.objects.all().delete()
    else:
        Region.objects.filter(pk=system.region.pk).update(status="suspended")
        CollectionJob.objects.update(
            status="failed", last_error="provider_authentication_failed", attempts=1
        )
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    assert not CollectionJob.objects.filter(status="pending").exists()


@pytest.mark.django_db(transaction=True)
def test_unexpected_canonical_solver_failure_retries_without_another_provider_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    def fail(binding: SiteBinding, target_date: object, data: ProviderData) -> dict[str, object]:
        raise RuntimeError("private solver state")

    for _ in range(3):
        assert not collect(job.pk, FakeAdapter(), fail, job.available_at)
        job.refresh_from_db()
        assert job.last_error == "calculation_failure"
    assert (job.status, job.attempts) == ("failed", 3)
    assert RequestAttempt.objects.filter(purpose="forecast").count() == 1
    assert "private solver" not in caplog.text
