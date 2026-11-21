import logging
from datetime import datetime, timedelta
from functools import partial
from itertools import takewhile
from typing import Any, cast

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import BooleanField, Exists, ExpressionWrapper, F, OuterRef, Q, QuerySet
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
from pyracast.forecasts.timing import default_times
from pyracast.incidents.models import IncidentRevision, PlantIncident
from pyracast.metering.models import (
    EpiasCollectionJob,
    EpiasConnection,
    EpiasConnectionRevision,
    EpiasSnapshot,
)
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    NotificationDeliveryError,
    NotificationDeliveryStatus,
    ReminderRecipient,
    ReminderRecipientTie,
    ReminderSchedule,
)
from pyracast.plants.calibration_runs import SupersedeReason, supersede_open_runs
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
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
    location_coordinate,
)

RETENTION = timedelta(days=30)
PURGE_BATCH_SIZE = 100
PURGE_CHUNK_SIZE = 500
PURGE_STATEMENT_BUDGET = 200

logger = logging.getLogger(__name__)
_deferrals = Deferrals(BACK_OFF)


def with_restore_facts(regions: QuerySet[Region]) -> QuerySet[Region]:
    """Annotates `location_taken`, which `restore_blocker` reads; restore holds the tenant lock
    while it reads it, so no location writer can interleave."""
    located = regions.annotate(
        location_latitude=location_coordinate("latitude"),
        location_longitude=location_coordinate("longitude"),
    )
    occupants = (
        Region.objects.active()
        .filter(tenant_id=OuterRef("tenant_id"))
        .exclude(pk=OuterRef("pk"))
        .annotate(
            location_latitude=location_coordinate("latitude"),
            location_longitude=location_coordinate("longitude"),
        )
        .filter(
            location_latitude=OuterRef("location_latitude"),
            location_longitude=OuterRef("location_longitude"),
        )
    )
    return located.annotate(location_taken=Exists(occupants))


def restore_blocker(region: Region, now: datetime) -> ErrorCode | None:
    if region.purge_started_at is not None or (
        region.deleted_at is not None and now >= region.deleted_at + RETENTION
    ):
        return ErrorCode.TRASH_RETENTION_EXPIRED
    if region.__dict__["location_taken"]:
        return ErrorCode.LOCATION_ALREADY_EXISTS
    return None


def _cancel_calibrations(plants: list[int], now: datetime) -> None:
    supersede_open_runs(plants, SupersedeReason.TRASHED, now)


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
        sending = deliveries.filter(status=NotificationDeliveryStatus.SENDING)
        sending.filter(transmit_started_at__isnull=True).update(
            status=NotificationDeliveryStatus.CANCELLED,
            last_error=NotificationDeliveryError.DEFINITION_TRASHED,
            lease_token=None,
            lease_expires_at=None,
        )
        # Transmission may have begun: the row keeps its lease so the late outcome still lands.
        sending.update(
            status=NotificationDeliveryStatus.UNCERTAIN,
            last_error=NotificationDeliveryError.DEFINITION_TRASHED,
        )
        record_audit(
            actor=actor, region=region, action=AuditAction.REGION_TRASHED, revision=region.revision
        )
        return region


def restore_region(actor: User, tenant_id: int, region_id: int) -> Region:
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        region = (
            with_restore_facts(Region.objects.filter(tenant_id=tenant_id))
            .select_for_update(of=("self",))
            .get(pk=region_id)
        )
        if region.deleted_at is None:
            return region
        now = timezone.now()
        if (blocker := restore_blocker(region, now)) is not None:
            raise DomainError(blocker)
        region.deleted_at = region.purge_warning_sent_at = None
        region.save(update_fields=("deleted_at", "purge_warning_sent_at"))
        CollectionSchedule.objects.get_or_create(
            region=region,
            defaults={
                "enabled": True,
                "times": default_times(),
                "effective_from": now,
            },
        )
        CollectionSchedule.objects.filter(region=region).update(effective_from=now)
        ReminderSchedule.objects.filter(region=region).update(
            effective_from=now, revision=F("revision") + 1
        )
        record_audit(
            actor=actor, region=region, action=AuditAction.REGION_RESTORED, revision=region.revision
        )
        return region


class _BudgetSpentError(Exception):
    pass


class _Budget:
    def __init__(self, statements: int) -> None:
        self.statements = statements

    def spend(self) -> None:
        if self.statements <= 0:
            raise _BudgetSpentError
        self.statements -= 1


def _delete(queryset: QuerySet[Any], budget: _Budget) -> None:
    """Delete in primary-key chunks without Django's collector, which reads whole rows.

    Every dependant must be deleted first, including Django-level cascades; the deferred
    foreign keys reject a forgotten one when the purge transaction commits.
    """
    manager = queryset.model._default_manager
    while identifiers := list(queryset.values_list("pk", flat=True)[:PURGE_CHUNK_SIZE]):
        budget.spend()
        manager.filter(pk__in=identifiers)._raw_delete(queryset.db)


def _delete_leaves_first(queryset: QuerySet[Any], reference: str, budget: _Budget) -> None:
    """Delete rows of a self-referencing model that no remaining row references, so a budget
    cut between chunks never leaves a reference to a deleted row. Revisions only point at
    older rows, so the references form chains that always have a leaf."""
    manager = queryset.model._default_manager
    referenced = manager.filter(**{reference: OuterRef("pk")})
    _delete(queryset.filter(~Exists(referenced)), budget)


def _detach(queryset: QuerySet[Any], budget: _Budget, **values: object) -> None:
    """The queryset must stop matching a row once its references are cleared."""
    manager = queryset.model._default_manager
    while identifiers := list(queryset.values_list("pk", flat=True)[:PURGE_CHUNK_SIZE]):
        budget.spend()
        manager.filter(pk__in=identifiers).update(**values)


def _purge_epias_history(plant: Plant, budget: _Budget) -> None:
    _delete(EpiasSnapshot.objects.filter(connection__plant=plant), budget)
    _delete(EpiasCollectionJob.objects.filter(connection__plant=plant), budget)
    _delete(EpiasConnectionRevision.objects.filter(connection__plant=plant), budget)
    _delete(EpiasConnection.objects.filter(plant=plant), budget)


def _purge_plant(plant: Plant, budget: _Budget) -> None:
    from pyracast.change_requests.models import ChangeRequest, IncidentChange, ProductionChangeItem

    _delete(ProductionChangeItem.objects.filter(request__plant=plant), budget)
    _delete(IncidentChange.objects.filter(request__plant=plant), budget)
    _delete(ChangeRequest.objects.filter(plant=plant), budget)
    _delete(IncidentRevision.objects.filter(incident__plant=plant), budget)
    _delete(PlantIncident.objects.filter(plant=plant), budget)
    _delete(ForecastIssuance.objects.filter(plant=plant), budget)
    _detach(
        CalibrationRun.objects.filter(plant=plant, comparison_activation__isnull=False),
        budget,
        comparison_activation=None,
    )
    _delete(CalibrationActivation.objects.filter(plant=plant), budget)
    _delete(CalibrationRun.objects.filter(plant=plant), budget)
    _delete_leaves_first(ProductionObservation.objects.filter(plant=plant), "supersedes", budget)
    _purge_epias_history(plant, budget)
    _delete(PlantEvent.objects.filter(plant=plant), budget)
    _detach(
        Plant.objects.filter(pk=plant.pk, current_configuration__isnull=False),
        budget,
        current_configuration=None,
    )
    _delete(PlantSubmission.objects.filter(plant=plant), budget)
    _delete(PlantConfiguration.objects.filter(plant=plant), budget)
    _delete(Plant.objects.filter(pk=plant.pk), budget)


def _purge_region(region: Region, plants: list[Plant], budget: _Budget) -> None:
    """Delete children before parents, so every committed prefix of the purge is consistent."""
    accounts = ProviderAccount.objects.filter(sitebinding__region=region).order_by("pk").distinct()
    list(
        ProviderAccount.objects.select_for_update()
        .filter(pk__in=accounts.values("pk"))
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    for plant in plants:
        _purge_plant(plant, budget)
    _delete(DeliveryRecipient.objects.filter(delivery__schedule__region=region), budget)
    _delete(NotificationDelivery.objects.filter(schedule__region=region), budget)
    _delete(ReminderRecipientTie.objects.filter(recipient__schedule__region=region), budget)
    _delete(ReminderRecipient.objects.filter(schedule__region=region), budget)
    _delete(ReminderSchedule.objects.filter(region=region), budget)
    _delete(CollectionSchedule.objects.filter(region=region), budget)
    _detach(
        RequestAttempt.objects.filter(Q(binding__region=region) | Q(job__binding__region=region)),
        budget,
        job=None,
        binding=None,
    )
    _delete(ReferenceCapacityNormalization.objects.filter(target_binding__region=region), budget)
    _delete(ForecastResult.objects.filter(snapshot__job__binding__region=region), budget)
    _delete(ForecastSnapshot.objects.filter(job__binding__region=region), budget)
    _delete(CollectionJob.objects.filter(binding__region=region), budget)
    _delete(HistoricalReferenceImport.objects.filter(binding__region=region), budget)
    _detach(
        Region.objects.filter(pk=region.pk, approved_binding__isnull=False),
        budget,
        status=RegionStatus.PENDING,
        approved_binding=None,
    )
    _delete(SiteBinding.objects.filter(region=region), budget)
    _delete(AuditEvent.objects.filter(region=region), budget)
    _delete(RegionLocationChangeRequest.objects.filter(region=region), budget)
    _delete(Region.objects.filter(pk=region.pk), budget)


def trashed_within_retention(now: datetime, remaining: timedelta) -> Q:
    return Q(deleted_at__lte=now - (RETENTION - remaining), deleted_at__gt=now - RETENTION)


def _purgeable(cutoff: datetime) -> Q:
    return Q(deleted_at__lte=cutoff) | Q(purge_started_at__isnull=False)


def purge_expired(now: datetime | None = None) -> dict[str, int]:
    """Purge expired regions within one statement budget; a region that exceeds it resumes
    on the next call before any region that has not started."""
    moment = timezone.now() if now is None else now
    if moment.utcoffset() is None:
        raise ValidationError("An aware timestamp is required.")
    cutoff = moment - RETENTION
    waiting = ExpressionWrapper(Q(purge_started_at__isnull=True), output_field=BooleanField())
    regions = list(
        Region.objects.filter(_purgeable(cutoff))
        .exclude(pk__in=_deferrals.active(moment))
        .order_by(waiting, "pk")
        .values_list("pk", flat=True)[:PURGE_BATCH_SIZE]
    )
    budget = _Budget(PURGE_STATEMENT_BUDGET)
    purged = isolate(
        takewhile(lambda _: budget.statements > 0, regions),
        partial(_purge_expired_region, moment=moment, budget=budget),
        logger=logger,
        event="retention_purge_failed",
        name=lambda region_id: f"region:{region_id}",
        back_off=lambda region_id: _deferrals.defer(region_id, moment),
    )
    done = [plants for plants in purged if plants is not None]
    return {"regions": len(done), "plants": sum(done)}


def _purge_expired_region(region_id: int, moment: datetime, budget: _Budget) -> int | None:
    with transaction.atomic():
        region = (
            Region.objects.select_for_update()
            .filter(_purgeable(moment - RETENTION), pk=region_id)
            .first()
        )
        if region is None:
            return None
        plants = list(Plant.objects.select_for_update().filter(region=region).order_by("pk"))
        if region.purge_started_at is None:
            region.purge_started_at = moment
            region.purge_plant_ids = [plant.pk for plant in plants]
            region.purge_bindings = _bindings(region)
            region.save(update_fields=("purge_started_at", "purge_plant_ids", "purge_bindings"))
        record = _purge_record(region)
        try:
            _purge_region(region, plants, budget)
        except _BudgetSpentError:
            return None
        transaction.on_commit(lambda: logger.info(record))
        return len(cast(list[int], region.purge_plant_ids))


def _bindings(region: Region) -> list[str]:
    """Names what the purge removes for the operator's runbook, never a name, place or key."""
    return [
        f"{provider}:{account}:{site}"
        for provider, account, site in SiteBinding.objects.filter(region=region)
        .order_by("pk")
        .values_list("provider_account__provider", "provider_account_id", "site_id")
    ]


def _purge_record(region: Region) -> str:
    deleted_at = cast(datetime, region.deleted_at).isoformat()
    plant_ids = ",".join(str(plant_id) for plant_id in cast(list[int], region.purge_plant_ids))
    bindings = ",".join(cast(list[str], region.purge_bindings))
    return (
        f"trash_purged kind=region region_id={region.pk} tenant_id={region.tenant_id} "
        f"deleted_at={deleted_at} plant_ids=[{plant_ids}] bindings=[{bindings}]"
    )
