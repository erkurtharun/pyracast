from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pyracast.accounts.operations import require_member
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
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
from pyracast.regions.models import AuditEvent, Region

RETENTION = timedelta(days=30)
PURGE_BATCH_SIZE = 100


def _restore_deadline(deleted_at: datetime | None, now: datetime) -> None:
    if deleted_at is not None and now >= deleted_at + RETENTION:
        raise ValidationError("trash_retention_expired")


def _cancel_calibrations(plants: list[int], now: datetime) -> None:
    CalibrationRun.objects.filter(plant_id__in=plants, status__in=("pending", "running")).update(
        status="superseded", completed_at=now, lease_token=None, lease_expires_at=None
    )


def trash_region(actor: User, tenant_id: int, region_id: int) -> Region:
    require_member(actor, tenant_id)
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
        if region.deleted_at is not None:
            return region
        plants = list(Plant.objects.select_for_update().filter(region=region).order_by("pk"))
        now = timezone.now()
        region.deleted_at = now
        region.save(update_fields=("deleted_at",))
        CollectionJob.objects.filter(
            binding__region=region, status__in=("pending", "retry", "running")
        ).update(status="stale", lease_token=None, lease_expires_at=None)
        _cancel_calibrations([plant.pk for plant in plants], now)
        deliveries = NotificationDelivery.objects.filter(preference__region=region)
        deliveries.filter(status__in=("pending", "retry")).update(
            status="cancelled",
            last_error="definition_trashed",
            lease_token=None,
            lease_expires_at=None,
        )
        deliveries.filter(status="sending").update(
            status="uncertain",
            last_error="definition_trashed",
            lease_token=None,
            lease_expires_at=None,
        )
        AuditEvent.objects.create(
            actor=actor, region=region, action="region_trashed", revision=region.revision
        )
        return region


def restore_region(actor: User, tenant_id: int, region_id: int) -> Region:
    require_member(actor, tenant_id)
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
        now = timezone.now()
        _restore_deadline(region.deleted_at, now)
        if region.deleted_at is None:
            return region
        region.deleted_at = None
        region.save(update_fields=("deleted_at",))
        CollectionSchedule.objects.get_or_create(
            region=region,
            defaults={
                "enabled": True,
                "times": [settings.PYRACAST_CUTOFF_LOCAL.strftime("%H:%M")],
                "effective_from": now,
            },
        )
        CollectionSchedule.objects.filter(region=region).update(effective_from=now)
        ReminderPreference.objects.filter(region=region).update(effective_from=now)
        AuditEvent.objects.create(
            actor=actor, region=region, action="region_restored", revision=region.revision
        )
        return region


def trash_plant(actor: User, tenant_id: int, region_id: int, plant_id: int) -> Plant:
    require_member(actor, tenant_id)
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
        if plant.deleted_at is not None:
            return plant
        if region.deleted_at is not None:
            raise ValidationError("parent_region_trashed")
        now = timezone.now()
        plant.deleted_at = now
        plant.save(update_fields=("deleted_at",))
        _cancel_calibrations([plant.pk], now)
        PlantEvent.objects.create(plant=plant, actor=actor, action="plant_trashed")
        return plant


def restore_plant(actor: User, tenant_id: int, region_id: int, plant_id: int) -> Plant:
    require_member(actor, tenant_id)
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
        if region.deleted_at is not None:
            raise ValidationError("parent_region_trashed")
        _restore_deadline(plant.deleted_at, timezone.now())
        if plant.deleted_at is None:
            return plant
        plant.deleted_at = None
        plant.save(update_fields=("deleted_at",))
        PlantEvent.objects.create(plant=plant, actor=actor, action="plant_restored")
        return plant


def _purge_plant(plant: Plant) -> None:
    PlantForecast.objects.filter(configuration__plant=plant).delete()
    CalibrationRun.objects.filter(plant=plant).update(comparison_activation=None)
    CalibrationActivation.objects.filter(plant=plant).delete()
    CalibrationRun.objects.filter(plant=plant).delete()
    ProductionObservation.objects.filter(plant=plant).delete()
    PlantEvent.objects.filter(plant=plant).delete()
    plant.current_configuration = None
    plant.save(update_fields=("current_configuration",))
    PlantConfiguration.objects.filter(plant=plant).delete()
    plant.delete()


def _purge_region(region: Region, plants: list[Plant]) -> None:
    accounts = ProviderAccount.objects.filter(sitebinding__region=region).order_by("pk").distinct()
    list(
        ProviderAccount.objects.select_for_update()
        .filter(pk__in=accounts.values("pk"))
        .order_by("pk")
    )
    for plant in plants:
        _purge_plant(plant)
    NotificationDelivery.objects.filter(preference__region=region).delete()
    ReminderPreference.objects.filter(region=region).delete()
    CollectionSchedule.objects.filter(region=region).delete()
    RequestAttempt.objects.filter(
        Q(binding__region=region) | Q(job__binding__region=region)
    ).update(job=None, binding=None)
    ForecastResult.objects.filter(snapshot__job__binding__region=region).delete()
    ForecastSnapshot.objects.filter(job__binding__region=region).delete()
    CollectionJob.objects.filter(binding__region=region).delete()
    region.approved_binding = None
    region.save(update_fields=("approved_binding",))
    SiteBinding.objects.filter(region=region).delete()
    AuditEvent.objects.filter(region=region).delete()
    region.delete()


def purge_expired(now: datetime | None = None) -> dict[str, int]:
    moment = timezone.now() if now is None else now
    if moment.utcoffset() is None:
        raise ValidationError("An aware timestamp is required.")
    cutoff = moment - RETENTION
    counts = {"regions": 0, "plants": 0}
    regions = list(
        Region.objects.filter(deleted_at__lte=cutoff)
        .order_by("pk")
        .values_list("pk", flat=True)[:PURGE_BATCH_SIZE]
    )
    for region_id in regions:
        with transaction.atomic():
            region = (
                Region.objects.select_for_update()
                .filter(pk=region_id, deleted_at__lte=cutoff)
                .first()
            )
            if region is None:
                continue
            plants = list(Plant.objects.select_for_update().filter(region=region).order_by("pk"))
            _purge_region(region, plants)
            counts["regions"] += 1
            counts["plants"] += len(plants)
    plants_to_purge = list(
        Plant.objects.filter(deleted_at__lte=cutoff)
        .order_by("pk")
        .values_list("pk", "region_id")[:PURGE_BATCH_SIZE]
    )
    for plant_id, region_id in plants_to_purge:
        with transaction.atomic():
            Region.objects.select_for_update().filter(pk=region_id).first()
            plant = (
                Plant.objects.select_for_update()
                .filter(pk=plant_id, deleted_at__lte=cutoff)
                .first()
            )
            if plant is None:
                continue
            _purge_plant(plant)
            counts["plants"] += 1
    return counts
