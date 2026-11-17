import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.db.models import QuerySet
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.historical_import import import_historical_reference
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    ForecastResult,
    ForecastSnapshot,
    HistoricalReferenceImport,
    RequestAttempt,
)
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    ProductionObservation,
)
from pyracast.plants.operations import create_plant
from pyracast.regions.models import AuditEvent, Region
from pyracast.trash import (
    RETENTION,
    purge_expired,
    restore_region,
    trash_region,
)
from tests.support.domain import NOW, System
from tests.support.historical_reference_import import power_file, source_settings, system_for_import
from tests.support.plant_operations import make_plant, request_run, snapshot_for
from tests.support.trash_operations import sibling

pytestmark = pytest.mark.django_db(transaction=True)


def test_region_purge_removes_imported_reference_archive_and_preserves_other_owners(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system = system_for_import(monkeypatch)
    record = import_historical_reference(
        system.admin,
        system.tenant.pk,
        system.region.pk,
        system.binding.pk,
        power_file(tmp_path),
        source_settings(),
        trusted_root=tmp_path,
    )
    assert ForecastSnapshot.objects.filter(job__reference_import=record).exists()
    protected_records = []
    for other_tenant in (False, True):
        region, binding = sibling(system)
        if other_tenant:
            region.tenant = Tenant.objects.create(name="Other tenant")
            region.save(update_fields=("tenant",))
        other_import = HistoricalReferenceImport.objects.create(
            binding=binding,
            sha256="b" * 64,
            source_name="other-reference.json",
            source_settings={},
            imported_by=system.admin,
            imported_at=NOW,
            interval_count=1,
        )
        job = CollectionJob.objects.create(
            binding=binding,
            reference_import=other_import,
            endpoint="historical_import",
            scheduled_slot=NOW,
            target_date=NOW.date(),
            available_at=NOW,
        )
        snapshot = ForecastSnapshot.objects.create(
            job=job,
            binding=binding,
            raw_payload={},
            normalized_intervals=[],
            collected_at=NOW,
            source_kind="estimated_actuals",
        )
        protected_records.append((region.pk, binding.pk, other_import.pk, job.pk, snapshot.pk))
    trash_region(system.member, system.tenant.pk, system.region.pk)
    assert purge_expired(NOW + RETENTION) == {"regions": 1, "plants": 0}
    assert not Region.objects.filter(pk=system.region.pk).exists()
    assert not SiteBinding.objects.filter(pk=system.binding.pk).exists()
    assert not HistoricalReferenceImport.objects.filter(pk=record.pk).exists()
    assert not ForecastSnapshot.objects.filter(job__reference_import=record.pk).exists()
    for region_id, binding_id, import_id, job_id, snapshot_id in protected_records:
        assert Region.objects.filter(pk=region_id).exists()
        assert SiteBinding.objects.filter(pk=binding_id).exists()
        assert HistoricalReferenceImport.objects.filter(pk=import_id).exists()
        assert CollectionJob.objects.filter(pk=job_id).exists()
        assert ForecastSnapshot.objects.filter(pk=snapshot_id).exists()


def linked_records(system: System, plant: Plant) -> tuple[ForecastSnapshot, ReminderSchedule]:
    configuration = plant.configurations.get()
    source = snapshot_for(system, NOW.date(), power_kw=0.4)
    source.job.status = "succeeded"
    source.job.save()
    ForecastResult.objects.create(snapshot=source, payload={"complete": True}, published_at=NOW)
    run = request_run(system, plant)
    CalibrationActivation.objects.create(
        plant=plant, configuration=configuration, calibration=run, actor=system.member
    )
    ProductionObservation.objects.create(
        plant=plant,
        configuration=configuration,
        starts_at=NOW - timedelta(days=1),
        ends_at=NOW - timedelta(hours=23),
        energy_mwh=1,
        quality="valid",
        source="meter",
        recorded_by=system.member,
    )
    RequestAttempt.objects.create(
        provider_account=system.account,
        binding=system.binding,
        job=source.job,
        utc_day=NOW.date(),
        created_at=NOW,
        purpose="forecasts",
        outcome="succeeded",
    )
    schedule = ReminderSchedule.objects.create(
        configured_by=system.member,
        region=system.region,
        enabled=True,
        times=["16:00"],
        effective_from=NOW - timedelta(days=1),
    )
    for index, status in enumerate(("pending", "retry", "sending", "sent", "uncertain")):
        NotificationDelivery.objects.create(
            schedule=schedule,
            snapshot=source,
            binding=system.binding,
            status=status,
            prepared_message={"kind": "current"} if status == "sending" else None,
            scheduled_for=NOW + timedelta(minutes=index),
            preparation_deadline=NOW + timedelta(minutes=index),
            available_at=NOW,
            lease_token=uuid.uuid4(),
            lease_expires_at=NOW + timedelta(minutes=5),
        )
    CollectionSchedule.objects.create(
        region=system.region, enabled=True, times=["16:00"], effective_from=NOW - timedelta(days=1)
    )
    return source, schedule


def test_region_trash_preserves_history_and_invalidates_pending_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source, schedule = linked_records(system, plant)
    run = CalibrationRun.objects.get()
    CalibrationRun.objects.filter(pk=run.pk).update(
        status="running", lease_token=uuid.uuid4(), lease_expires_at=NOW + timedelta(minutes=5)
    )
    CollectionJob.objects.exclude(pk=source.job_id).update(
        status="running", lease_token=uuid.uuid4(), lease_expires_at=NOW + timedelta(minutes=5)
    )
    region = trash_region(system.member, system.tenant.pk, system.region.pk)
    assert region.deleted_at == NOW
    assert region.status == "approved"
    assert region.approved_binding_id == system.binding.pk
    assert Plant.objects.filter(pk=plant.pk).exists()
    assert ForecastSnapshot.objects.get(pk=source.pk).raw_payload == source.raw_payload
    assert CollectionJob.objects.get(pk=source.job_id).status == "succeeded"
    stale = CollectionJob.objects.exclude(pk=source.job_id).get()
    assert stale.status == "stale"
    assert stale.lease_token is None
    assert stale.lease_expires_at is None
    run.refresh_from_db()
    assert run.status == "superseded"
    assert run.lease_token is None
    assert run.lease_expires_at is None
    assert run.completed_at == NOW
    states = list(NotificationDelivery.objects.order_by("scheduled_for"))
    assert [row.status for row in states] == [
        "cancelled",
        "cancelled",
        "uncertain",
        "sent",
        "uncertain",
    ]
    assert all(row.lease_token is None and row.lease_expires_at is None for row in states[:3])
    assert states[3].lease_token is not None
    schedule.refresh_from_db()
    assert schedule.enabled
    assert CollectionSchedule.objects.get(region=region).times == ["16:00"]
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=1))
    assert trash_region(system.member, system.tenant.pk, region.pk).deleted_at == NOW
    assert AuditEvent.objects.filter(region=region, action="region_trashed").count() == 1


def test_restore_is_idempotent_and_restarts_future_schedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    _, schedule = linked_records(system, plant)
    args = (system.member, system.tenant.pk, system.region.pk)
    assert restore_region(*args).deleted_at is None
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=1))
    trash_region(*args)
    restored_at = NOW + timedelta(days=2)
    monkeypatch.setattr(timezone, "now", lambda: restored_at)
    region = restore_region(*args)
    assert region.deleted_at is None
    assert CollectionSchedule.objects.get(region=region).effective_from == restored_at
    schedule.refresh_from_db()
    assert schedule.effective_from == restored_at
    assert schedule.times == ["16:00"]
    assert CalibrationRun.objects.get().status == "superseded"
    assert restore_region(*args).deleted_at is None
    assert AuditEvent.objects.filter(region=region, action="region_restored").count() == 1


def test_restore_creates_missing_schedule_without_recovering_trashed_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    args = (system.member, system.tenant.pk, system.region.pk)
    assert not CollectionSchedule.objects.exists()
    trash_region(*args)
    restored_at = NOW + timedelta(days=1, hours=1)
    monkeypatch.setattr(timezone, "now", lambda: restored_at)
    restore_region(*args)
    schedule = CollectionSchedule.objects.get(region=system.region)
    assert schedule.enabled
    assert schedule.times == [settings.PYRACAST_CUTOFF_LOCAL.strftime("%H:%M")]
    assert schedule.effective_from == restored_at
    assert enqueue_due(restored_at) == []


def test_restore_deadline_is_strict_and_purge_boundary_is_inclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    args = (system.member, system.tenant.pk, system.region.pk)
    trash_region(*args)
    assert purge_expired(NOW + RETENTION - timedelta(microseconds=1)) == {"regions": 0, "plants": 0}
    monkeypatch.setattr(timezone, "now", lambda: NOW + RETENTION)
    with pytest.raises(ValidationError, match="trash_retention_expired"):
        restore_region(*args)
    assert purge_expired() == {"regions": 1, "plants": 1}
    assert not Plant.objects.filter(pk=plant.pk).exists()
    assert not Region.objects.filter(pk=system.region.pk).exists()
    assert purge_expired() == {"regions": 0, "plants": 0}


def test_purge_region_removes_owned_dependencies_and_preserves_shared_quota_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source, _ = linked_records(system, plant)
    region, binding = sibling(system)
    other_plant = create_plant(
        system.member, system.tenant.pk, region.pk, name="Other plant", dc_capacity_mw=2
    )
    other_job = CollectionJob.objects.create(
        binding=binding, target_date=NOW.date(), scheduled_slot=NOW, available_at=NOW
    )
    other_attempt = RequestAttempt.objects.create(
        provider_account=system.account,
        binding=binding,
        job=other_job,
        utc_day=NOW.date(),
        created_at=NOW,
        purpose="forecasts",
    )
    all_attempts = RequestAttempt.objects.count()
    owned_attempt_ids = list(
        RequestAttempt.objects.filter(binding=system.binding).values_list("pk", flat=True)
    )
    provider_audits = AuditEvent.objects.filter(provider_account=system.account).count()
    trash_region(system.member, system.tenant.pk, system.region.pk)
    assert purge_expired(NOW + RETENTION) == {"regions": 1, "plants": 1}
    assert not Region.objects.filter(pk=system.region.pk).exists()
    assert not SiteBinding.objects.filter(pk=system.binding.pk).exists()
    assert not ForecastSnapshot.objects.filter(pk=source.pk).exists()
    assert not ForecastResult.objects.exists()
    assert not CalibrationRun.objects.exists()
    assert not CalibrationActivation.objects.exists()
    assert not ProductionObservation.objects.exists()
    assert not ReminderSchedule.objects.exists()
    assert not NotificationDelivery.objects.exists()
    assert not CollectionSchedule.objects.exists()
    assert not PlantEvent.objects.filter(plant_id=plant.pk).exists()
    assert not PlantConfiguration.objects.filter(plant_id=plant.pk).exists()
    assert Region.objects.filter(pk=region.pk).exists()
    assert Plant.objects.filter(pk=other_plant.pk).exists()
    assert CollectionJob.objects.filter(pk=other_job.pk).exists()
    assert SiteBinding.objects.filter(pk=binding.pk).exists()
    other_attempt.refresh_from_db()
    assert other_attempt.job_id == other_job.pk
    assert other_attempt.binding_id == binding.pk
    assert RequestAttempt.objects.count() == all_attempts
    assert RequestAttempt.objects.filter(
        pk__in=owned_attempt_ids, binding=None, job=None
    ).count() == len(owned_attempt_ids)
    assert system.account.requestattempt_set.count() == all_attempts
    assert AuditEvent.objects.filter(provider_account=system.account).count() == provider_audits


def test_purge_removes_a_calibration_that_compared_an_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    linked_records(system, plant)
    previous = CalibrationRun.objects.get(plant=plant)
    CalibrationRun.objects.create(
        plant=plant,
        configuration=previous.configuration,
        binding=previous.binding,
        requested_by=system.member,
        window_start=previous.window_start,
        window_end=previous.window_end,
        cutoff_at=NOW,
        status="rejected",
        comparison_activation=CalibrationActivation.objects.get(plant=plant),
    )
    trash_region(system.member, system.tenant.pk, system.region.pk)
    assert purge_expired(NOW + RETENTION) == {"regions": 1, "plants": 1}
    assert not CalibrationRun.objects.exists()
    assert not CalibrationActivation.objects.exists()


@pytest.mark.parametrize("operation", [trash_region, restore_region])
def test_lifecycle_requires_membership_and_exact_tenant_scope(
    monkeypatch: pytest.MonkeyPatch, operation: Callable[[User, int, int], Region]
) -> None:
    system, _ = make_plant(monkeypatch)
    other = Tenant.objects.create(name="Other")
    user = User.objects.create_user(username="outsider")
    Membership.objects.create(user=user, tenant=other, role=WorkspaceRole.ADMIN)
    with pytest.raises(PermissionDenied):
        operation(user, system.tenant.pk, system.region.pk)
    with pytest.raises(Region.DoesNotExist):
        operation(user, other.pk, system.region.pk)


def test_concurrent_trash_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _ = make_plant(monkeypatch)
    barrier = Barrier(2)

    def remove() -> datetime | None:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return trash_region(system.member, system.tenant.pk, system.region.pk).deleted_at
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: remove(), range(2)))
    assert results == [NOW, NOW]
    assert AuditEvent.objects.filter(region=system.region, action="region_trashed").count() == 1


def test_concurrent_purge_removes_each_definition_once(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _ = make_plant(monkeypatch)
    trash_region(system.member, system.tenant.pk, system.region.pk)
    barrier = Barrier(2)
    original = Region.objects.select_for_update

    def synchronized_lock() -> QuerySet[Region]:
        barrier.wait(timeout=10)
        return original()

    monkeypatch.setattr(Region.objects, "select_for_update", synchronized_lock)

    def purge() -> dict[str, int]:
        close_old_connections()
        try:
            return purge_expired(NOW + RETENTION)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: purge(), range(2)))
    assert sum(item["plants"] for item in results) == 1
    assert sum(item["regions"] for item in results) == 1


def test_purge_validates_clock_and_bounds_each_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="aware"):
        purge_expired(NOW.replace(tzinfo=None))
    system, plant = make_plant(monkeypatch)
    region, _ = sibling(system)
    trash_region(system.member, system.tenant.pk, system.region.pk)
    trash_region(system.member, system.tenant.pk, region.pk)
    monkeypatch.setattr("pyracast.trash.PURGE_BATCH_SIZE", 1)
    assert purge_expired(NOW + RETENTION) == {"regions": 1, "plants": 1}
    assert Region.objects.filter(pk=region.pk).exists()
    assert purge_expired(NOW + RETENTION) == {"regions": 1, "plants": 0}
    assert not Plant.objects.filter(pk=plant.pk).exists()
