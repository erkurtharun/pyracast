import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from threading import Barrier, Event
from typing import Any, cast

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection, connections
from django.utils import timezone

from pyracast.epias import EpiasClient, EpiasDay, EpiasError, EpiasPlant, EpiasReading
from pyracast.forecasts.models import CollectionSchedule, RequestAttempt
from pyracast.metering.jobs import (
    LEASE_DURATION,
    claim_epias_job,
    pending_epias_ids,
    run_epias_job,
)
from pyracast.metering.models import (
    EpiasCollectionJob,
    EpiasConnection,
    EpiasConnectionRevision,
    EpiasSnapshot,
)
from pyracast.metering.operations import (
    connect_epias,
    disconnect_epias,
    get_epias_status,
)
from pyracast.metering.scheduler import enqueue_due_epias_jobs
from pyracast.plants.models import Plant, PlantEvent
from pyracast.regions.models import Region
from pyracast.trash import purge_expired
from tests.support.domain import NOW
from tests.support.errors import raises_code
from tests.support.metering import (
    NEXT_NIGHT,
    PROVIDER_PLANT,
    FixtureClient,
    connect,
    connected,
    day_data,
)
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)


def test_connection_scope_identity_and_missing_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client = FixtureClient()
    stranger = User.objects.create(username="stranger")
    with pytest.raises(PermissionDenied):
        connect(plant, stranger, client)
    with pytest.raises(PermissionDenied):
        get_epias_status(plant.pk, actor=stranger)
    with pytest.raises(PermissionDenied):
        disconnect_epias(plant.pk, actor=stranger)
    assert client.calls == 0
    assert get_epias_status(plant.pk, actor=system.member).connected is False
    disconnect_epias(plant.pk, actor=system.admin)
    for kwargs, error in (
        ({"provider_plant_id": 0}, "invalid_epias_plant"),
        ({"provider_plant_id": True}, "invalid_epias_plant"),
        ({"expected_eic": ""}, "invalid_epias_plant"),
        ({"provider_plant_id": 100}, "epias_plant_identity_mismatch"),
        ({"expected_eic": "wrong"}, "epias_plant_identity_mismatch"),
    ):
        values: dict[str, object] = {"provider_plant_id": 7, "expected_eic": PROVIDER_PLANT.eic}
        values.update(kwargs)
        with raises_code(error):
            connect_epias(
                plant.pk,
                actor=system.admin,
                client=cast(EpiasClient, client),
                provider_plant_id=cast(int, values["provider_plant_id"]),
                expected_eic=cast(str, values["expected_eic"]),
            )
    Plant.objects.filter(pk=plant.pk).update(current_configuration=None)
    with raises_code("configuration_unavailable"):
        connect(plant, system.admin)
    Region.objects.filter(pk=system.region.pk).update(deleted_at=NOW)
    with pytest.raises(Plant.DoesNotExist):
        get_epias_status(plant.pk, actor=system.member)


@pytest.mark.parametrize("change", ["approval", "region", "configuration", "admin"])
def test_connection_rechecks_changes_after_validation(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    system, plant = make_plant(monkeypatch)

    def revoke() -> None:
        if change == "approval":
            Region.objects.filter(pk=system.region.pk).update(status="suspended")
        elif change == "region":
            Region.objects.filter(pk=system.region.pk).update(deleted_at=NOW)
        elif change == "admin":
            User.objects.filter(pk=system.admin.pk).update(is_superuser=False)
        else:
            Plant.objects.filter(pk=plant.pk).update(current_configuration=None)

    if change == "admin":
        with pytest.raises(PermissionDenied):
            connect(plant, system.admin, FixtureClient(revoke))
    else:
        with raises_code("configuration_unavailable"):
            connect(plant, system.admin, FixtureClient(revoke))
    assert not EpiasConnection.objects.exists()


def test_daily_reconciliation_preserves_acquisitions_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, linked = connected(monkeypatch)
    assert EpiasCollectionJob.objects.count() == 90
    assert {j.target_date for j in EpiasCollectionJob.objects.all()} == {
        NOW.date() - timedelta(days=i) for i in range(1, 91)
    }
    initial = EpiasCollectionJob.objects.order_by("-target_date").first()
    assert initial is not None
    requests = RequestAttempt.objects.count()
    assert run_epias_job(initial.pk, fetch=day_data, now=NOW)
    assert not run_epias_job(initial.pk, fetch=day_data, now=NOW)
    next_day = NEXT_NIGHT
    identifiers = enqueue_due_epias_jobs(now=next_day)
    assert len(identifiers) == 7
    assert enqueue_due_epias_jobs(now=next_day) == identifiers
    revised = EpiasCollectionJob.objects.get(pk=identifiers[1])
    assert revised.target_date == initial.target_date
    assert run_epias_job(revised.pk, fetch=day_data, now=next_day)
    acquisitions = EpiasSnapshot.objects.filter(target_date=initial.target_date).order_by(
        "available_at"
    )
    assert acquisitions.count() == 2
    assert acquisitions[0].content_sha256 == acquisitions[1].content_sha256
    assert acquisitions[0].readings[0] == {
        "provider_timestamp": datetime.combine(
            initial.target_date, datetime.min.time(), tzinfo=timezone.get_current_timezone()
        ).isoformat(),
        "hour": "00:00",
        "period_start": datetime.combine(
            initial.target_date, datetime.min.time(), tzinfo=timezone.get_current_timezone()
        ).isoformat(),
        "period_end": datetime.combine(
            initial.target_date, datetime.min.time(), tzinfo=timezone.get_current_timezone()
        )
        .replace(hour=1)
        .isoformat(),
        "total_mwh": "1.5",
        "sun_mwh": None,
    }
    status = get_epias_status(plant.pk, actor=system.member)
    assert status.archived_days == 1
    assert status.last_success_at is not None
    assert RequestAttempt.objects.count() == requests
    disconnect_epias(plant.pk, actor=system.admin)
    assert get_epias_status(plant.pk, actor=system.member).connected is False
    event = PlantEvent.objects.get(plant=plant, action="epias_disconnected")
    assert event.actor_id == system.admin.pk
    assert event.details == {"provider_plant_id": 7, "eic": linked.eic, "revision": 1}
    assert enqueue_due_epias_jobs(now=next_day) == []
    assert EpiasSnapshot.objects.filter(pk=acquisitions[1].pk).exists()
    monkeypatch.setattr(timezone, "now", lambda: next_day + timedelta(hours=1))
    reconnected = connect(plant, system.admin)
    assert reconnected.revision == linked.revision + 1
    assert PlantEvent.objects.filter(plant=plant, action="epias_connected").count() == 2
    assert EpiasCollectionJob.objects.filter(status="superseded").exists()
    assert EpiasCollectionJob.objects.filter(status="pending").count() == 90


def test_scheduler_respects_slots_and_rejects_naive_time(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant, linked = connected(monkeypatch)
    with pytest.raises(ValidationError, match="aware timestamp"):
        enqueue_due_epias_jobs(now=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="aware timestamp"):
        claim_epias_job(1, NOW.replace(tzinfo=None))
    assert enqueue_due_epias_jobs(now=NOW - timedelta(hours=2)) == []
    schedule = CollectionSchedule.objects.create(
        region=system.region, enabled=False, times=["16:00"], effective_from=NOW
    )
    assert enqueue_due_epias_jobs(now=NOW + timedelta(days=1)) == []
    schedule.enabled = True
    schedule.times = ["18:00", "16:00"]
    schedule.effective_from = NOW + timedelta(days=2)
    schedule.save()
    assert enqueue_due_epias_jobs(now=NOW + timedelta(days=1)) == []
    schedule.effective_from = NOW - timedelta(days=1)
    schedule.save()
    EpiasConnection.objects.filter(pk=linked.pk).update(updated_at=NOW + timedelta(days=2))
    assert enqueue_due_epias_jobs(now=NOW + timedelta(days=1)) == []
    EpiasConnection.objects.filter(pk=linked.pk).update(updated_at=NOW)
    Region.objects.filter(pk=plant.region_id).update(deleted_at=NOW)
    assert enqueue_due_epias_jobs(now=NOW + timedelta(days=1)) == []


def test_two_schedulers_create_only_seven_daily_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    connected(monkeypatch)
    barrier = Barrier(2)

    def enqueue() -> list[int]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return enqueue_due_epias_jobs(now=NEXT_NIGHT)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: enqueue(), range(2)))
    assert results[0] == results[1]
    assert EpiasCollectionJob.objects.count() == 97


def test_daily_slot_uses_region_night_independently_of_forecast_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _, _ = connected(monkeypatch)
    Region.objects.filter(pk=system.region.pk).update(timezone="America/New_York")
    CollectionSchedule.objects.create(region=system.region, times=["14:00"], effective_from=NOW)
    before_slot = NEXT_NIGHT + timedelta(hours=4, minutes=59)
    after_slot = NEXT_NIGHT + timedelta(hours=5)
    assert enqueue_due_epias_jobs(now=before_slot) == []
    first = enqueue_due_epias_jobs(now=after_slot)
    assert len(first) == 7
    assert enqueue_due_epias_jobs(now=after_slot + timedelta(minutes=1)) == first
    jobs = EpiasCollectionJob.objects.filter(pk__in=first)
    assert set(jobs.values_list("scheduled_for", flat=True)) == {after_slot}
    assert set(jobs.values_list("target_date", flat=True)) == {
        NEXT_NIGHT.date() - timedelta(days=offset) for offset in range(1, 8)
    }
    assert EpiasCollectionJob.objects.count() == 97


@pytest.mark.parametrize("nightly", [False, True])
def test_duplicate_workers_only_one_external_acquisition(
    monkeypatch: pytest.MonkeyPatch, nightly: bool
) -> None:
    connected(monkeypatch)
    identifiers = enqueue_due_epias_jobs(now=NEXT_NIGHT) if nightly else []
    job_id = (
        EpiasCollectionJob.objects.filter(pk__in=identifiers).first()
        if nightly
        else EpiasCollectionJob.objects.first()
    )
    assert job_id is not None
    started = Event()
    release = Event()

    def fetch(plant: EpiasPlant, day: date) -> EpiasDay:
        assert not connection.in_atomic_block
        started.set()
        assert release.wait(timeout=10)
        return day_data(plant, day)

    def work() -> bool:
        close_old_connections()
        try:
            return run_epias_job(job_id.pk, fetch=fetch, now=NEXT_NIGHT if nightly else NOW)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(work)
        assert started.wait(timeout=10)
        second = pool.submit(work)
        assert second.result(timeout=10) is False
        release.set()
        assert first.result(timeout=10)
    assert EpiasSnapshot.objects.count() == 1
    job_id.refresh_from_db()
    assert job_id.attempts == 1


@pytest.mark.parametrize("change", ["approval", "region", "configuration", "disconnect", "delete"])
def test_publication_rechecks_revocation_and_deletion(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    system, plant, linked = connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None

    def fetch(provider: EpiasPlant, day: date) -> EpiasDay:
        if change == "approval":
            Region.objects.filter(pk=system.region.pk).update(status="suspended")
        elif change == "region":
            Region.objects.filter(pk=system.region.pk).update(deleted_at=NOW)
        elif change == "configuration":
            Plant.objects.filter(pk=plant.pk).update(current_configuration=None)
        elif change == "disconnect":
            disconnect_epias(plant.pk, actor=system.admin)
        else:
            linked.delete()
        return day_data(provider, day)

    assert not run_epias_job(job.pk, fetch=fetch, now=NOW)
    assert EpiasSnapshot.objects.count() == 0
    if change != "delete":
        job.refresh_from_db()
        assert job.status == "superseded"


def test_expired_leases_exhaust_budget_and_ignore_old_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None
    assert claim_epias_job(0, NOW) is None
    lease = claim_epias_job(job.pk, NOW)
    assert lease is not None
    for index in (1, 2):
        assert (
            claim_epias_job(job.pk, NOW + (LEASE_DURATION + timedelta(seconds=1)) * index)
            is not None
        )
    assert claim_epias_job(job.pk, NOW + (LEASE_DURATION + timedelta(seconds=1)) * 3) is None
    job.refresh_from_db()
    assert job.status == "failed"
    assert job.attempts == 3
    other = EpiasCollectionJob.objects.exclude(pk=job.pk).first()
    assert other is not None
    EpiasCollectionJob.objects.filter(pk=other.pk).update(available_at=NOW + timedelta(hours=1))
    assert claim_epias_job(other.pk, NOW) is None
    assert other.pk not in pending_epias_ids(NOW, limit=100)


def test_provider_failures_are_redacted_bounded_and_honor_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _, linked = connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None

    def fail(plant: EpiasPlant, day: date) -> EpiasDay:
        raise EpiasError("sensitive credential", retry_after_seconds=300)

    now = NOW
    for attempt in range(1, 4):
        assert not run_epias_job(job.pk, fetch=fail, now=now)
        job.refresh_from_db()
        assert job.attempts == attempt
        assert job.last_error == "epias_provider_failure"
        assert job.available_at >= now + timedelta(seconds=300)
        now = job.available_at
    assert job.status == "failed"
    assert not run_epias_job(job.pk, fetch=fail, now=now)
    assert not EpiasSnapshot.objects.exists()
    assert get_epias_status(linked.plant_id, actor=system.member).last_success_at is None


def test_identity_mismatch_and_unexpected_errors_cannot_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected(monkeypatch)
    jobs = list(EpiasCollectionJob.objects.all())

    def wrong(plant: EpiasPlant, day: date) -> EpiasDay:
        return day_data(plant, day - timedelta(days=1))

    def failure(plant: EpiasPlant, day: date) -> EpiasDay:
        raise RuntimeError("password must never be stored")

    assert not run_epias_job(jobs[0].pk, fetch=wrong, now=NOW)
    assert not run_epias_job(jobs[1].pk, fetch=failure, now=NOW)
    assert set(
        EpiasCollectionJob.objects.filter(attempts=1).values_list("last_error", flat=True)
    ) == {"epias_provider_failure"}
    assert not EpiasSnapshot.objects.exists()


def test_revoked_pending_work_is_superseded_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _, _ = connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    assert not run_epias_job(job.pk, fetch=day_data, now=NOW)
    job.refresh_from_db()
    assert job.status == "superseded"
    assert job.attempts == 0


@pytest.mark.parametrize("empty", [True, False])
def test_partial_acquisitions_are_immutable_and_retry_at_most_three_times(
    monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None

    def partial(plant: EpiasPlant, day: date) -> EpiasDay:
        data = day_data(plant, day)
        rows = (
            ()
            if empty
            else (
                *data.readings[:-1],
                EpiasReading(data.readings[-1].timestamp, "23:00", None, Decimal("0")),
            )
        )
        return EpiasDay(plant, day, rows, NOW, json.dumps({"attempt": 1}))

    now = NOW
    for attempt in range(1, 4):
        assert not run_epias_job(job.pk, fetch=partial, now=now)
        job.refresh_from_db()
        assert EpiasSnapshot.objects.filter(job=job).count() == attempt
        assert job.last_error == "partial_data"
        if attempt < 3:
            assert job.status == "pending"
            assert job.available_at >= now + timedelta(minutes=30 * 2 ** (attempt - 1))
            assert not run_epias_job(job.pk, fetch=partial, now=now)
        now = job.available_at
    assert job.status == "failed"
    assert not EpiasSnapshot.objects.filter(job=job, complete=True).exists()
    assert not run_epias_job(job.pk, fetch=partial, now=now)


def test_partial_acquisition_can_be_completed_without_rewriting_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plant, _ = connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None

    def partial(provider: EpiasPlant, day: date) -> EpiasDay:
        data = day_data(provider, day)
        return EpiasDay(provider, day, data.readings[:1], NOW, '{"items": []}')

    assert not run_epias_job(job.pk, fetch=partial, now=NOW)
    first = EpiasSnapshot.objects.get(job=job)
    job.refresh_from_db()
    assert run_epias_job(job.pk, fetch=day_data, now=job.available_at)
    first.refresh_from_db()
    assert first.readings == [EpiasSnapshot.objects.get(job=job, attempt=2).readings[0]]
    assert not first.complete
    assert EpiasSnapshot.objects.get(job=job, attempt=2).complete
    assert EpiasSnapshot.objects.filter(connection__plant=plant).count() == 2


def test_reconnection_same_clock_starts_new_jobs_and_hides_previous_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, linked = connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None
    assert run_epias_job(job.pk, fetch=day_data, now=NOW)
    disconnect_epias(plant.pk, actor=system.admin)
    reconnected = connect(plant, system.admin)
    assert reconnected.revision == linked.revision + 1
    assert EpiasCollectionJob.objects.filter(status="pending").count() == 90
    assert get_epias_status(plant.pk, actor=system.member).archived_days == 1
    EpiasConnection.objects.filter(pk=linked.pk).update(provider_plant_id=100, eic="new-eic")
    assert get_epias_status(plant.pk, actor=system.member).archived_days == 0
    assert EpiasSnapshot.objects.count() == 1


@pytest.mark.parametrize("outcome", ["success", "failure", "delete_failure", "revoked_failure"])
def test_stale_or_revoked_attempts_cannot_finish(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    system, _, linked = connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None

    def fetch(plant: EpiasPlant, day: date) -> EpiasDay:
        if outcome == "delete_failure":
            linked.delete()
        elif outcome == "revoked_failure":
            Region.objects.filter(pk=system.region.pk).update(status="suspended")
        else:
            EpiasCollectionJob.objects.filter(pk=job.pk).update(lease_expires_at=NOW)
        if outcome != "success":
            raise EpiasError("private-error")
        return day_data(plant, day)

    assert not run_epias_job(job.pk, fetch=fetch, now=NOW)
    assert not EpiasSnapshot.objects.exists()
    if outcome == "revoked_failure":
        job.refresh_from_db()
        assert job.status == "superseded"


def test_missing_connection_status_is_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    status = get_epias_status(plant.pk, actor=system.member)
    assert asdict(status) == {
        "state": "never_connected",
        "connected": False,
        "disconnected_at": None,
        "health": None,
        "complete_through": None,
        "failed_days": 0,
        "provider_plant_id": None,
        "settlement_plant_id": None,
        "first_settlement_date": None,
        "last_settlement_date": None,
        "eic": "",
        "name": "",
        "last_success_at": None,
        "last_error": "",
        "pending_jobs": 0,
        "archived_days": 0,
    }


def test_job_wait_clock_prevents_expired_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None
    elapsed = 0.0

    def monotonic() -> float:
        return elapsed

    monkeypatch.setattr("pyracast.background.clock.time.monotonic", monotonic)

    def fetch(plant: EpiasPlant, day: date) -> EpiasDay:
        nonlocal elapsed
        elapsed = 301
        return day_data(plant, day)

    assert not run_epias_job(job.pk, fetch=fetch, now=NOW)
    assert not EpiasSnapshot.objects.exists()
    job.refresh_from_db()
    assert job.status == "running"
    assert job.lease_expires_at == NOW + LEASE_DURATION


@pytest.mark.parametrize("operation", ["claim", "schedule"])
@pytest.mark.parametrize("target", ["region", "connection"])
def test_collection_tolerates_purge_after_reading_work_identifiers(
    monkeypatch: pytest.MonkeyPatch, operation: str, target: str
) -> None:
    system, _, linked = connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None
    table = {
        "region": "regions_region",
        "connection": "metering_epiasconnection",
    }[target]
    armed = True

    def execute_with_purge(
        execute: Callable[..., Any],
        sql: str,
        params: object,
        many: bool,
        context: dict[str, object],
    ) -> Any:
        nonlocal armed
        if armed and sql.startswith("SELECT") and "FOR UPDATE" in sql and f'"{table}"' in sql:
            armed = False
            if target == "region":
                Region.objects.filter(pk=system.region.pk).update(
                    deleted_at=NOW - timedelta(days=31)
                )
                assert purge_expired(NOW)["regions"] == 1
            else:
                EpiasCollectionJob.objects.filter(connection=linked).delete()
                EpiasConnectionRevision.objects.filter(connection=linked).delete()
                linked.delete()
        return execute(sql, params, many, context)

    with connection.execute_wrapper(execute_with_purge):
        if operation == "claim":
            assert claim_epias_job(job.pk, NOW) is None
        else:
            assert enqueue_due_epias_jobs(now=NOW + timedelta(days=1)) == []
    assert not armed
    assert not EpiasCollectionJob.objects.exists()


def test_unexpected_metering_failure_logs_type_and_origin_not_message(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None

    def failure(plant: EpiasPlant, day: date) -> EpiasDay:
        raise RuntimeError("password must never be logged")

    assert not run_epias_job(job.pk, fetch=failure, now=NOW)
    [message] = [
        record.getMessage()
        for record in pyracast_logs.records
        if record.getMessage().startswith("metering_job_failed")
    ]
    assert message.startswith(
        f"metering_job_failed job={job.pk} attempt=1 error_type=RuntimeError "
        "origin=pyracast.metering.jobs:"
    )
    assert "password" not in pyracast_logs.text
