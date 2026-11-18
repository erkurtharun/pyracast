import logging
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import WorkspaceRole
from pyracast.background.failures import BACK_OFF, Deferrals, isolate
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.errors import DomainError, ErrorCode
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionJobStatus,
    CollectionSchedule,
    ForecastResult,
    ForecastSnapshot,
    HistoricalReferenceImport,
    ReferenceCapacityNormalization,
    RequestAttempt,
)
from pyracast.incidents.models import IncidentRevision, PlantIncident
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryError,
    NotificationDeliveryStatus,
    ReminderSchedule,
)
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    CalibrationStatus,
    ForecastIssuance,
    Plant,
    PlantConfiguration,
    PlantEvent,
    PlantSubmission,
    ProductionObservation,
)
from pyracast.regions.audit import record_audit
from pyracast.regions.models import (
    AuditAction,
    AuditEvent,
    Region,
    RegionLocationChangeRequest,
    RegionStatus,
)
from pyracast.regions.operations import require_available_location

RETENTION = timedelta(days=30)
PURGE_BATCH_SIZE = 100
PURGE_CHUNK_SIZE = 500

logger = logging.getLogger(__name__)
_deferrals = Deferrals(BACK_OFF)


def _restore_deadline(deleted_at: datetime | None, now: datetime) -> None:
    if deleted_at is not None and now >= deleted_at + RETENTION:
        raise DomainError(ErrorCode.TRASH_RETENTION_EXPIRED)


def _cancel_calibrations(plants: list[int], now: datetime) -> None:
    CalibrationRun.objects.filter(
        plant_id__in=plants, status__in=(CalibrationStatus.PENDING, CalibrationStatus.RUNNING)
    ).update(
        status=CalibrationStatus.SUPERSEDED,
        completed_at=now,
        lease_token=None,
        lease_expires_at=None,
    )


def trash_region(actor: User, tenant_id: int, region_id: int) -> Region:
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        region: Region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
        if region.deleted_at is not None:
            return region
        plants = list(Plant.objects.select_for_update().filter(region=region).order_by("pk"))
        now = timezone.now()
        region.deleted_at = now
        region.save(update_fields=("deleted_at",))
        CollectionJob.objects.filter(
            binding__region=region,
            status__in=(
                CollectionJobStatus.PENDING,
                CollectionJobStatus.RETRY,
                CollectionJobStatus.RUNNING,
            ),
        ).update(status=CollectionJobStatus.STALE, lease_token=None, lease_expires_at=None)
        _cancel_calibrations([plant.pk for plant in plants], now)
        deliveries = NotificationDelivery.objects.filter(schedule__region=region)
        deliveries.filter(
            status__in=(NotificationDeliveryStatus.PENDING, NotificationDeliveryStatus.RETRY)
        ).update(
            status=NotificationDeliveryStatus.CANCELLED,
            last_error=NotificationDeliveryError.DEFINITION_TRASHED,
            lease_token=None,
            lease_expires_at=None,
        )
        deliveries.filter(status=NotificationDeliveryStatus.SENDING).update(
            status=NotificationDeliveryStatus.UNCERTAIN,
            last_error=NotificationDeliveryError.DEFINITION_TRASHED,
            lease_token=None,
            lease_expires_at=None,
        )
        record_audit(
            actor=actor, region=region, action=AuditAction.REGION_TRASHED, revision=region.revision
        )
        return region


def restore_region(actor: User, tenant_id: int, region_id: int) -> Region:
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
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
        record_audit(
            actor=actor, region=region, action=AuditAction.REGION_RESTORED, revision=region.revision
        )
        return region


def _delete(queryset: QuerySet[Any]) -> None:
    """Delete in primary-key chunks without Django's collector, which reads whole rows.

    Every dependant must be deleted first, including Django-level cascades; the deferred
    foreign keys reject a forgotten one when the purge transaction commits.
    """
    manager = queryset.model._default_manager
    while identifiers := list(queryset.values_list("pk", flat=True)[:PURGE_CHUNK_SIZE]):
        manager.filter(pk__in=identifiers)._raw_delete(queryset.db)


def _purge_epias_history(plant: Plant) -> None:
    _delete(EpiasSnapshot.objects.filter(connection__plant=plant))
    _delete(EpiasCollectionJob.objects.filter(connection__plant=plant))
    _delete(EpiasConnection.objects.filter(plant=plant))


def _purge_plant(plant: Plant) -> None:
    from pyracast.change_requests.models import ChangeRequest, IncidentChange, ProductionChangeItem

    _delete(ProductionChangeItem.objects.filter(request__plant=plant))
    _delete(IncidentChange.objects.filter(request__plant=plant))
    _delete(ChangeRequest.objects.filter(plant=plant))
    _delete(IncidentRevision.objects.filter(incident__plant=plant))
    _delete(PlantIncident.objects.filter(plant=plant))
    _delete(ForecastIssuance.objects.filter(plant=plant))
    CalibrationRun.objects.filter(plant=plant).update(comparison_activation=None)
    _delete(CalibrationActivation.objects.filter(plant=plant))
    _delete(CalibrationRun.objects.filter(plant=plant))
    _delete(ProductionObservation.objects.filter(plant=plant))
    _purge_epias_history(plant)
    _delete(PlantEvent.objects.filter(plant=plant))
    plant.current_configuration = None
    plant.save(update_fields=("current_configuration",))
    _delete(PlantSubmission.objects.filter(plant=plant))
    _delete(PlantConfiguration.objects.filter(plant=plant))
    _delete(Plant.objects.filter(pk=plant.pk))


def _purge_region(region: Region, plants: list[Plant]) -> None:
    accounts = ProviderAccount.objects.filter(sitebinding__region=region).order_by("pk").distinct()
    list(
        ProviderAccount.objects.select_for_update()
        .filter(pk__in=accounts.values("pk"))
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    for plant in plants:
        _purge_plant(plant)
    _delete(NotificationDelivery.objects.filter(schedule__region=region))
    _delete(ReminderSchedule.objects.filter(region=region))
    _delete(CollectionSchedule.objects.filter(region=region))
    RequestAttempt.objects.filter(
        Q(binding__region=region) | Q(job__binding__region=region)
    ).update(job=None, binding=None)
    _delete(ReferenceCapacityNormalization.objects.filter(target_binding__region=region))
    _delete(ForecastResult.objects.filter(snapshot__job__binding__region=region))
    _delete(ForecastSnapshot.objects.filter(job__binding__region=region))
    _delete(CollectionJob.objects.filter(binding__region=region))
    _delete(HistoricalReferenceImport.objects.filter(binding__region=region))
    region.status = RegionStatus.PENDING
    region.approved_binding = None
    region.save(update_fields=("status", "approved_binding"))
    _delete(SiteBinding.objects.filter(region=region))
    _delete(AuditEvent.objects.filter(region=region))
    _delete(RegionLocationChangeRequest.objects.filter(region=region))
    _delete(Region.objects.filter(pk=region.pk))


def purge_expired(now: datetime | None = None) -> dict[str, int]:
    moment = timezone.now() if now is None else now
    if moment.utcoffset() is None:
        raise ValidationError("An aware timestamp is required.")
    cutoff = moment - RETENTION
    regions = list(
        Region.objects.filter(deleted_at__lte=cutoff)
        .exclude(pk__in=_deferrals.active(moment))
        .order_by("pk")
        .values_list("pk", flat=True)[:PURGE_BATCH_SIZE]
    )
    purged = isolate(
        regions,
        partial(_purge_expired_region, cutoff=cutoff),
        logger=logger,
        event="retention_purge_failed",
        name=lambda region_id: f"region:{region_id}",
        back_off=lambda region_id: _deferrals.defer(region_id, moment),
    )
    done = [plants for plants in purged if plants is not None]
    return {"regions": len(done), "plants": sum(done)}


def _purge_expired_region(region_id: int, cutoff: datetime) -> int | None:
    with transaction.atomic():
        region = (
            Region.objects.select_for_update().filter(pk=region_id, deleted_at__lte=cutoff).first()
        )
        if region is None:
            return None
        plants = list(Plant.objects.select_for_update().filter(region=region).order_by("pk"))
        _purge_region(region, plants)
        return len(plants)
