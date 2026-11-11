from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from pyracast.epias import EpiasDay, EpiasError, EpiasPlant
from pyracast.forecasts.models import CollectionSchedule
from pyracast.metering.jobs import EpiasLease, claim_epias_job, pending_epias_ids, run_epias_job
from pyracast.metering.models import EpiasCollectionJob, EpiasSnapshot
from pyracast.metering.scheduler import enqueue_due_epias_jobs
from pyracast.plants.models import CalibrationRun
from tests.test_metering import NEXT_NIGHT, connected, day_data

pytestmark = pytest.mark.django_db(transaction=True)


def nightly_job(monkeypatch: pytest.MonkeyPatch) -> EpiasCollectionJob:
    connected(monkeypatch)
    identifiers = enqueue_due_epias_jobs(now=NEXT_NIGHT)
    return EpiasCollectionJob.objects.get(
        pk__in=identifiers, target_day=NEXT_NIGHT.date() - timedelta(days=1)
    )


def test_three_am_slot_and_nine_am_deadline_are_independent_of_forecasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _, _ = connected(monkeypatch)
    assert not EpiasCollectionJob.objects.filter(retry_until__isnull=False).exists()
    CollectionSchedule.objects.create(
        region=system.region, times=[], effective_from=NEXT_NIGHT - timedelta(hours=1)
    )
    assert enqueue_due_epias_jobs(now=NEXT_NIGHT - timedelta(minutes=1)) == []
    identifiers = enqueue_due_epias_jobs(now=NEXT_NIGHT)
    assert len(identifiers) == 7
    jobs = EpiasCollectionJob.objects.filter(pk__in=identifiers)
    assert set(jobs.values_list("scheduled_for", flat=True)) == {NEXT_NIGHT}
    nightly = jobs.get(target_day=NEXT_NIGHT.date() - timedelta(days=1))
    assert nightly.target_day == NEXT_NIGHT.date() - timedelta(days=1)
    assert nightly.retry_until == NEXT_NIGHT + timedelta(hours=6)
    assert set(jobs.values_list("retry_until", flat=True)) == {NEXT_NIGHT + timedelta(hours=6)}
    assert enqueue_due_epias_jobs(now=NEXT_NIGHT + timedelta(hours=6)) == []
    assert not run_epias_job(nightly.pk, fetch=day_data, now=NEXT_NIGHT + timedelta(hours=6))
    nightly.refresh_from_db()
    assert nightly.status == "failed"
    assert nightly.last_error == "retry_window_expired"
    assert nightly.attempts == 0
    jobs.delete()
    assert enqueue_due_epias_jobs(now=NEXT_NIGHT + timedelta(hours=6)) == []
    assert EpiasCollectionJob.objects.count() == 90


@pytest.mark.parametrize("empty", [True, False])
def test_nightly_missing_data_retries_every_half_hour_through_morning(
    monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    job = nightly_job(monkeypatch)
    monkeypatch.setattr("pyracast.metering.jobs.time.monotonic", lambda: 0.0)

    def partial(plant: EpiasPlant, day: date) -> EpiasDay:
        data = day_data(plant, day)
        return replace(data, readings=() if empty else data.readings[:1])

    now = NEXT_NIGHT
    for attempt in range(1, 13):
        assert not run_epias_job(job.pk, fetch=partial, now=now)
        job.refresh_from_db()
        assert job.attempts == attempt
        assert job.next_attempt_at == now + timedelta(minutes=30)
        assert job.status == ("pending" if attempt < 12 else "failed")
        assert EpiasSnapshot.objects.filter(job=job).count() == attempt
        assert not run_epias_job(job.pk, fetch=partial, now=now + timedelta(minutes=29))
        now = job.next_attempt_at
    assert job.pk not in pending_epias_ids(now)
    assert not run_epias_job(job.pk, fetch=partial, now=now)
    assert not EpiasSnapshot.objects.filter(job=job, complete=True).exists()


@pytest.mark.parametrize("retry_after", [None, 60, 2700, 86400])
def test_nightly_transport_failures_honor_half_hour_and_longer_retry_after(
    monkeypatch: pytest.MonkeyPatch, retry_after: int | None
) -> None:
    job = nightly_job(monkeypatch)
    monkeypatch.setattr("pyracast.metering.jobs.time.monotonic", lambda: 0.0)

    def fail(plant: EpiasPlant, day: date) -> EpiasDay:
        raise EpiasError("private credential", retry_after_seconds=retry_after)

    assert not run_epias_job(job.pk, fetch=fail, now=NEXT_NIGHT)
    job.refresh_from_db()
    assert job.next_attempt_at == NEXT_NIGHT + timedelta(seconds=max(1800, retry_after or 0))
    assert job.status == ("failed" if retry_after == 86400 else "pending")
    assert job.last_error == "epias_provider_failure"
    assert not EpiasSnapshot.objects.filter(job=job).exists()


@pytest.mark.parametrize("transport", [False, True])
def test_older_daily_reconciliation_retains_three_attempt_cap(
    monkeypatch: pytest.MonkeyPatch, transport: bool
) -> None:
    job = nightly_job(monkeypatch)
    job = EpiasCollectionJob.objects.get(
        connection=job.connection,
        scheduled_for=NEXT_NIGHT,
        target_day=job.target_day - timedelta(days=1),
    )
    monkeypatch.setattr("pyracast.metering.jobs.time.monotonic", lambda: 0.0)

    def fetch(plant: EpiasPlant, day: date) -> EpiasDay:
        if transport:
            raise EpiasError("request_failed")
        return replace(day_data(plant, day), readings=())

    now = NEXT_NIGHT
    for attempt in range(1, 4):
        assert not run_epias_job(job.pk, fetch=fetch, now=now)
        job.refresh_from_db()
        assert job.attempts == attempt
        assert job.next_attempt_at == now + timedelta(minutes=30)
        assert job.status == ("pending" if attempt < 3 else "failed")
        now = job.next_attempt_at
    assert not run_epias_job(job.pk, fetch=fetch, now=now)


def test_nightly_expired_lease_waits_half_hour_and_dispatch_expires_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = nightly_job(monkeypatch)
    assert claim_epias_job(job.pk, NEXT_NIGHT) is not None
    assert claim_epias_job(job.pk, NEXT_NIGHT + timedelta(minutes=6)) is None
    assert job.pk not in pending_epias_ids(NEXT_NIGHT + timedelta(minutes=6))
    assert claim_epias_job(job.pk, NEXT_NIGHT + timedelta(minutes=31)) is not None
    assert job.retry_until is not None
    assert job.pk not in pending_epias_ids(job.retry_until)
    job.refresh_from_db()
    assert job.status == "failed"
    assert job.last_error == "retry_window_expired"
    assert job.attempts == 2
    assert job.pk not in pending_epias_ids(job.retry_until + timedelta(hours=1))


@pytest.mark.parametrize("during_request", [False, True])
def test_disabling_schedule_stops_nightly_requests_and_publication(
    monkeypatch: pytest.MonkeyPatch, during_request: bool
) -> None:
    job = nightly_job(monkeypatch)
    schedule = CollectionSchedule.objects.create(
        region=job.connection.plant.region,
        enabled=True,
        times=["16:00"],
        effective_from=NEXT_NIGHT - timedelta(hours=1),
    )
    if not during_request:
        schedule.enabled = False
        schedule.save()

    def fetch(plant: EpiasPlant, day: date) -> EpiasDay:
        assert during_request
        schedule.enabled = False
        schedule.save()
        return day_data(plant, day)

    assert not run_epias_job(job.pk, fetch=fetch, now=NEXT_NIGHT)
    job.refresh_from_db()
    assert job.status == "superseded"
    assert job.attempts == int(during_request)
    assert not EpiasSnapshot.objects.filter(job=job).exists()


def test_nightly_complete_data_stops_retries_without_matched_reference_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = nightly_job(monkeypatch)

    assert run_epias_job(job.pk, fetch=day_data, now=NEXT_NIGHT)
    job.refresh_from_db()
    assert job.status == "succeeded"
    assert not run_epias_job(job.pk, fetch=day_data, now=NEXT_NIGHT + timedelta(minutes=30))
    assert EpiasSnapshot.objects.filter(job=job, complete=True).count() == 1
    assert not CalibrationRun.objects.exists()


def test_deadline_reached_after_claim_prevents_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = nightly_job(monkeypatch)
    assert job.retry_until is not None
    elapsed = 0.0
    requested = False
    monkeypatch.setattr("pyracast.metering.jobs.time", SimpleNamespace(monotonic=lambda: elapsed))

    def claim_then_delay(job_id: int, now: datetime) -> EpiasLease | None:
        nonlocal elapsed
        lease = claim_epias_job(job_id, now)
        elapsed = 2.0
        return lease

    def fetch(plant: EpiasPlant, day: date) -> EpiasDay:
        nonlocal requested
        requested = True
        return day_data(plant, day)

    monkeypatch.setattr("pyracast.metering.jobs.claim_epias_job", claim_then_delay)

    assert not run_epias_job(job.pk, fetch=fetch, now=job.retry_until - timedelta(seconds=1))
    assert not requested
    job.refresh_from_db()
    assert job.status == "failed"
    assert job.last_error == "retry_window_expired"
    assert job.attempts == 1
    assert job.lease_token is None
    assert job.lease_expires_at is None
    assert not EpiasSnapshot.objects.filter(job=job).exists()


@pytest.mark.parametrize("complete", [False, True])
def test_response_started_before_deadline_can_archive_after_nine(
    monkeypatch: pytest.MonkeyPatch, complete: bool
) -> None:
    job = nightly_job(monkeypatch)
    assert job.retry_until is not None
    elapsed = 0.0
    monkeypatch.setattr("pyracast.metering.jobs.time.monotonic", lambda: elapsed)

    def fetch(plant: EpiasPlant, day: date) -> EpiasDay:
        nonlocal elapsed
        elapsed = 2.0
        data = day_data(plant, day)
        return data if complete else replace(data, readings=())

    assert (
        run_epias_job(job.pk, fetch=fetch, now=job.retry_until - timedelta(seconds=1)) is complete
    )
    job.refresh_from_db()
    assert job.status == ("succeeded" if complete else "failed")
    snapshot = EpiasSnapshot.objects.get(job=job)
    assert snapshot.available_at == job.retry_until + timedelta(seconds=1)
    assert snapshot.complete is complete


@pytest.mark.parametrize("offset", [-1, 360, 500])
def test_settlement_identity_lookup_avoids_requests_outside_night_window(
    monkeypatch: pytest.MonkeyPatch, offset: int
) -> None:
    connected(monkeypatch)

    def unexpected_client() -> None:
        pytest.fail("Settlement identity lookup must wait for the nightly window.")

    monkeypatch.setattr("pyracast.metering.scheduler.get_epias_client", unexpected_client)
    assert enqueue_due_epias_jobs(now=NEXT_NIGHT + timedelta(minutes=offset)) == []
