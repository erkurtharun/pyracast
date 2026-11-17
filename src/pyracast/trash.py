from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import Membership
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    ForecastResult,
    ForecastSnapshot,
    HistoricalReferenceImport,
    ReferenceCapacityNormalization,
    RequestAttempt,
)
from pyracast.incidents.models import IncidentRevision, PlantIncident
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    ForecastIssuance,
    Plant,
    PlantConfiguration,
    PlantEvent,
    ProductionObservation,
)
from pyracast.regions.models import AuditEvent, Region
from pyracast.regions.operations import require_available_location

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
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=Membership.Role.ADMIN, lock=True)
        region: Region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
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
        deliveries = NotificationDelivery.objects.filter(schedule__region=region)
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
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=Membership.Role.ADMIN, lock=True)
        location = Region.objects.get(pk=region_id, tenant_id=tenant_id)
        require_available_location(
            tenant_id, location.latitude, location.longitude, exclude_region_id=region_id
        )
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
        ReminderSchedule.objects.filter(region=region).update(effective_from=now)
        AuditEvent.objects.create(
            actor=actor, region=region, action="region_restored", revision=region.revision
        )
        return region


def _purge_epias_history(plant: Plant) -> None:
    EpiasSnapshot.objects.filter(connection__plant=plant).delete()
    EpiasCollectionJob.objects.filter(connection__plant=plant).delete()
    EpiasConnection.objects.filter(plant=plant).delete()


def _purge_plant(plant: Plant) -> None:
    from pyracast.change_requests.models import ChangeRequest

    ChangeRequest.objects.filter(plant=plant).delete()
    IncidentRevision.objects.filter(incident__plant=plant).delete()
    PlantIncident.objects.filter(plant=plant).delete()
    ForecastIssuance.objects.filter(plant=plant).delete()
    CalibrationRun.objects.filter(plant=plant).update(comparison_activation=None)
    CalibrationActivation.objects.filter(plant=plant).delete()
    CalibrationRun.objects.filter(plant=plant).delete()
    ProductionObservation.objects.filter(plant=plant).delete()
    _purge_epias_history(plant)
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
    NotificationDelivery.objects.filter(schedule__region=region).delete()
    ReminderSchedule.objects.filter(region=region).delete()
    CollectionSchedule.objects.filter(region=region).delete()
    RequestAttempt.objects.filter(
        Q(binding__region=region) | Q(job__binding__region=region)
    ).update(job=None, binding=None)
    ReferenceCapacityNormalization.objects.filter(target_binding__region=region).delete()
    ForecastResult.objects.filter(snapshot__job__binding__region=region).delete()
    ForecastSnapshot.objects.filter(job__binding__region=region).delete()
    CollectionJob.objects.filter(binding__region=region).delete()
    HistoricalReferenceImport.objects.filter(binding__region=region).delete()
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
    return counts
