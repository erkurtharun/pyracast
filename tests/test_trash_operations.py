import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from typing import Literal

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.db.models import QuerySet
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.notifications.models import NotificationDelivery, ReminderPreference
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    PlantForecast,
    ProductionObservation,
)
from pyracast.plants.operations import create_plant
from pyracast.regions.models import AuditEvent, Region
from pyracast.trash import (
    RETENTION,
    purge_expired,
    restore_plant,
    restore_region,
    trash_plant,
    trash_region,
)
from tests.test_domain import NOW, System
from tests.test_plant_operations import make_plant, request_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def sibling(system: System) -> tuple[Region, SiteBinding]:
    region = Region.objects.create(
        tenant=system.tenant,
        name="Other region",
        latitude=40,
        longitude=29,
        timezone=system.region.timezone,
        status="approved",
    )
    binding = SiteBinding.objects.get(pk=system.binding.pk)
    binding.pk = None
    binding.region = region
    binding.save()
    region.approved_binding = binding
    region.save()
    return region, binding


def linked_records(system: System, plant: Plant) -> tuple[ForecastSnapshot, ReminderPreference]:
    configuration = plant.configurations.get()
    source = snapshot_for(system, NOW.date(), power_kw=0.4)
    source.job.status = "succeeded"
    source.job.save()
    ForecastResult.objects.create(snapshot=source, payload={"complete": True}, published_at=NOW)
    run = request_run(system, plant)
    CalibrationActivation.objects.create(
        plant=plant, configuration=configuration, calibration=run, actor=system.member
    )
    PlantForecast.objects.create(
        configuration=configuration,
        snapshot=source,
        calibration=run,
        resolution_minutes=30,
        target_date=NOW.date(),
        payload={},
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
    preference = ReminderPreference.objects.create(
        user=system.member,
        region=system.region,
        enabled=True,
        times=["16:00"],
        effective_from=NOW - timedelta(days=1),
    )
    for index, status in enumerate(("pending", "retry", "sending", "sent", "uncertain")):
        NotificationDelivery.objects.create(
            preference=preference,
            snapshot=source,
            status=status,
            scheduled_for=NOW + timedelta(minutes=index),
            available_at=NOW,
            lease_token=uuid.uuid4(),
            lease_expires_at=NOW + timedelta(minutes=5),
        )
    CollectionSchedule.objects.create(
        region=system.region, enabled=True, times=["16:00"], effective_from=NOW - timedelta(days=1)
    )
    return source, preference


def test_region_trash_preserves_history_and_invalidates_pending_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source, preference = linked_records(system, plant)
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
    assert Plant.objects.get(pk=plant.pk).deleted_at is None
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
    preference.refresh_from_db()
    assert preference.enabled
    assert CollectionSchedule.objects.get(region=region).times == ["16:00"]
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=1))
    assert trash_region(system.member, system.tenant.pk, region.pk).deleted_at == NOW
    assert AuditEvent.objects.filter(region=region, action="region_trashed").count() == 1


def test_restore_parent_preserves_individual_plant_trash_and_restarts_future_schedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    _, preference = linked_records(system, plant)
    active = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="Active", dc_capacity_mw=1
    )
    args = (system.member, system.tenant.pk, system.region.pk)
    assert restore_region(*args).deleted_at is None
    assert restore_plant(*args, plant.pk).deleted_at is None
    trashed = trash_plant(*args, plant.pk)
    assert trashed.deleted_at == NOW
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=1))
    assert trash_plant(*args, plant.pk).deleted_at == NOW
    trash_region(*args)
    with pytest.raises(ValidationError, match="parent_region_trashed"):
        restore_plant(*args, plant.pk)
    restored_at = NOW + timedelta(days=2)
    monkeypatch.setattr(timezone, "now", lambda: restored_at)
    region = restore_region(*args)
    assert region.deleted_at is None
    assert Plant.objects.get(pk=plant.pk).deleted_at == NOW
    assert Plant.objects.get(pk=active.pk).deleted_at is None
    assert CollectionSchedule.objects.get(region=region).effective_from == restored_at
    preference.refresh_from_db()
    assert preference.effective_from == restored_at
    assert preference.times == ["16:00"]
    assert restore_plant(*args, plant.pk).deleted_at is None
    assert PlantEvent.objects.filter(plant=plant, action="plant_trashed").count() == 1
    assert PlantEvent.objects.filter(plant=plant, action="plant_restored").count() == 1
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


def test_inherited_plant_trash_cannot_start_an_independent_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    args = (system.member, system.tenant.pk, system.region.pk)
    trash_region(*args)
    with pytest.raises(ValidationError, match="parent_region_trashed"):
        trash_plant(*args, plant.pk)
    plant.refresh_from_db()
    assert plant.deleted_at is None
    assert not PlantEvent.objects.filter(plant=plant, action="plant_trashed").exists()


@pytest.mark.parametrize("kind", ["region", "plant"])
def test_restore_deadline_is_strict_and_purge_boundary_is_inclusive(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    system, plant = make_plant(monkeypatch)
    args = (system.member, system.tenant.pk, system.region.pk)
    if kind == "region":
        trash_region(*args)
    else:
        trash_plant(*args, plant.pk)
    assert purge_expired(NOW + RETENTION - timedelta(microseconds=1)) == {"regions": 0, "plants": 0}
    monkeypatch.setattr(timezone, "now", lambda: NOW + RETENTION)
    if kind == "region":
        with pytest.raises(ValidationError, match="trash_retention_expired"):
            restore_region(*args)
    else:
        with pytest.raises(ValidationError, match="trash_retention_expired"):
            restore_plant(*args, plant.pk)
    assert purge_expired() == {"regions": int(kind == "region"), "plants": 1}
    assert not Plant.objects.filter(pk=plant.pk).exists()
    assert Region.objects.filter(pk=system.region.pk).exists() == (kind == "plant")
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
    assert not PlantForecast.objects.exists()
    assert not CalibrationRun.objects.exists()
    assert not CalibrationActivation.objects.exists()
    assert not ProductionObservation.objects.exists()
    assert not ReminderPreference.objects.exists()
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


def test_purge_plant_keeps_region_history_and_reminders(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    source, preference = linked_records(system, plant)
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
    args = (system.member, system.tenant.pk, system.region.pk)
    trash_plant(*args, plant.pk)
    assert CollectionJob.objects.get(pk=source.job_id).status == "succeeded"
    assert NotificationDelivery.objects.filter(status="pending").exists()
    assert purge_expired(NOW + RETENTION) == {"regions": 0, "plants": 1}
    assert Region.objects.filter(pk=system.region.pk).exists()
    assert SiteBinding.objects.filter(pk=system.binding.pk).exists()
    assert ForecastSnapshot.objects.filter(pk=source.pk).exists()
    assert ForecastResult.objects.filter(snapshot=source).exists()
    assert ReminderPreference.objects.filter(pk=preference.pk).exists()
    assert NotificationDelivery.objects.count() == 5


@pytest.mark.parametrize(
    "action", ["trash_region", "restore_region", "trash_plant", "restore_plant"]
)
def test_lifecycle_requires_membership_and_exact_tenant_scope(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    system, plant = make_plant(monkeypatch)
    other = Tenant.objects.create(name="Other")
    user = User.objects.create_user(username="outsider")
    Membership.objects.create(user=user, tenant=other)
    region_actions = {"trash_region": trash_region, "restore_region": restore_region}
    if action in region_actions:
        operation = region_actions[action]
        with pytest.raises(PermissionDenied):
            operation(user, system.tenant.pk, system.region.pk)
        with pytest.raises(Region.DoesNotExist):
            operation(user, other.pk, system.region.pk)
    else:
        plant_operation = trash_plant if action == "trash_plant" else restore_plant
        with pytest.raises(PermissionDenied):
            plant_operation(user, system.tenant.pk, system.region.pk, plant.pk)
        with pytest.raises(Region.DoesNotExist):
            plant_operation(user, other.pk, system.region.pk, plant.pk)
        region, _ = sibling(system)
        with pytest.raises(Plant.DoesNotExist):
            plant_operation(system.member, system.tenant.pk, region.pk, plant.pk)


@pytest.mark.parametrize("kind", ["region", "plant"])
def test_concurrent_trash_is_idempotent(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    system, plant = make_plant(monkeypatch)
    barrier = Barrier(2)

    def remove() -> datetime | None:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            args = (system.member, system.tenant.pk, system.region.pk)
            if kind == "region":
                return trash_region(*args).deleted_at
            return trash_plant(*args, plant.pk).deleted_at
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: remove(), range(2)))
    assert results == [NOW, NOW]
    if kind == "region":
        assert AuditEvent.objects.filter(region=system.region, action="region_trashed").count() == 1
    else:
        assert PlantEvent.objects.filter(plant=plant, action="plant_trashed").count() == 1


@pytest.mark.parametrize("kind", ["region", "plant"])
def test_concurrent_purge_removes_each_definition_once(
    monkeypatch: pytest.MonkeyPatch, kind: Literal["region", "plant"]
) -> None:
    system, plant = make_plant(monkeypatch)
    args = (system.member, system.tenant.pk, system.region.pk)
    if kind == "region":
        trash_region(*args)
    else:
        trash_plant(*args, plant.pk)
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
    assert sum(item["regions"] for item in results) == int(kind == "region")


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
