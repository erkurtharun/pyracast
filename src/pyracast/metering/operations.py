from dataclasses import dataclass
from datetime import date, datetime

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Max, Min, Q, QuerySet
from django.utils import timezone

from pyracast.accounts.authorization import require_platform_admin, tenant_membership
from pyracast.epias import EpiasClient
from pyracast.errors import DomainError, ErrorCode
from pyracast.metering.client import get_epias_client
from pyracast.metering.models import (
    EpiasCollectionJob,
    EpiasConnection,
    EpiasConnectionRevision,
    EpiasHealth,
    EpiasJobStatus,
    EpiasSnapshot,
    EpiasSourceKind,
    EpiasState,
)
from pyracast.plants.events import record_plant_event
from pyracast.plants.models import Plant, PlantEventAction
from pyracast.plants.operations import configuration_is_current
from pyracast.regions.models import Region


def member_plant(plant_id: int, actor: User) -> Plant:
    plant = Plant.objects.active().select_related("region").get(pk=plant_id)
    tenant_membership(actor, plant.region.tenant_id)
    return plant


def _lock_platform_admin(actor: User) -> None:
    # The person's account before the workspace's rows, the order of member removal and erasure;
    # the lock keeps the actor a platform administrator until the change commits. No key update:
    # a workspace writer that commits a row naming this person does not wait for it.
    require_platform_admin(User.objects.select_for_update(no_key=True).get(pk=actor.pk))


@dataclass(frozen=True)
class EpiasConnect:
    connection: EpiasConnection
    changed: bool


def connect_epias(
    plant_id: int,
    *,
    actor: User,
    provider_plant_id: int,
    expected_eic: str,
    client: EpiasClient | None = None,
) -> EpiasConnect:
    require_platform_admin(actor)
    plant = Plant.objects.active().select_related("region").get(pk=plant_id)
    if isinstance(provider_plant_id, bool) or provider_plant_id <= 0 or not expected_eic.strip():
        raise DomainError(ErrorCode.INVALID_EPIAS_PLANT)
    if plant.current_configuration is None or not configuration_is_current(
        plant.current_configuration
    ):
        raise DomainError(ErrorCode.CONFIGURATION_UNAVAILABLE)
    provider = client or get_epias_client()
    candidates = provider.list_plants()
    selected = next((p for p in candidates if p.id == provider_plant_id), None)
    if selected is None or selected.eic != expected_eic.strip():
        raise DomainError(ErrorCode.EPIAS_PLANT_IDENTITY_MISMATCH)
    settlement = provider.resolve_settlement_plant(selected.eic)
    with transaction.atomic():
        _lock_platform_admin(actor)
        Region.objects.select_for_update().get(pk=plant.region_id)
        plant = Plant.objects.select_for_update().get(pk=plant.pk)
        if (
            plant.region.deleted_at is not None
            or plant.current_configuration is None
            or not configuration_is_current(plant.current_configuration)
        ):
            raise DomainError(ErrorCode.CONFIGURATION_UNAVAILABLE)
        connection = EpiasConnection.objects.select_for_update().filter(plant=plant).first()
        settlement_id = settlement.id if settlement else None
        if connection is not None and (
            connection.enabled,
            connection.provider_plant_id,
            connection.eic,
            connection.settlement_plant_id,
        ) == (True, selected.id, selected.eic, settlement_id):
            return EpiasConnect(connection, changed=False)
        if connection is None:
            connection = EpiasConnection(plant=plant, connected_by=actor)
        else:
            connection.revision += 1
            EpiasCollectionJob.objects.filter(
                connection=connection,
                status__in=(EpiasJobStatus.PENDING, EpiasJobStatus.RUNNING),
            ).update(status=EpiasJobStatus.SUPERSEDED, lease_token=None, lease_expires_at=None)
        connection.provider_plant_id = selected.id
        connection.settlement_plant_id = settlement_id
        connection.settlement_checked_at = timezone.now()
        connection.eic = selected.eic
        connection.name = selected.name
        connection.connected_by = actor
        connection.enabled = True
        connection.save()
        event = record_plant_event(
            plant_id=plant.pk,
            actor=actor,
            action=PlantEventAction.EPIAS_CONNECTED,
            details={
                "provider_plant_id": connection.provider_plant_id,
                "eic": connection.eic,
                "revision": connection.revision,
            },
        )
        EpiasConnectionRevision.objects.create(
            connection=connection,
            revision=connection.revision,
            provider_plant_id=connection.provider_plant_id,
            eic=connection.eic,
            connected_at=event.created_at,
        )
        from pyracast.metering.scheduler import enqueue_connection

        enqueue_connection(connection, timezone.now(), initial=True)
        return EpiasConnect(connection, changed=True)


def disconnect_epias(plant_id: int, *, actor: User) -> bool:
    require_platform_admin(actor)
    plant = Plant.objects.active().select_related("region").get(pk=plant_id)
    with transaction.atomic():
        _lock_platform_admin(actor)
        Region.objects.select_for_update().get(pk=plant.region_id)
        Plant.objects.select_for_update().get(pk=plant.pk)
        connection = EpiasConnection.objects.select_for_update().filter(plant=plant).first()
        if connection is None or not connection.enabled:
            return False
        connection.enabled = False
        connection.save(update_fields=("enabled", "updated_at"))
        record_plant_event(
            plant_id=plant.pk,
            actor=actor,
            action=PlantEventAction.EPIAS_DISCONNECTED,
            details={
                "provider_plant_id": connection.provider_plant_id,
                "eic": connection.eic,
                "revision": connection.revision,
            },
        )
        EpiasCollectionJob.objects.filter(
            connection=connection,
            status__in=(EpiasJobStatus.PENDING, EpiasJobStatus.RUNNING),
        ).update(status=EpiasJobStatus.SUPERSEDED, lease_token=None, lease_expires_at=None)
    return True


@dataclass(frozen=True)
class EpiasStatus:
    state: EpiasState
    connected: bool
    disconnected_at: datetime | None
    health: EpiasHealth | None
    complete_through: date | None
    failed_days: int
    provider_plant_id: int | None
    settlement_plant_id: int | None
    first_settlement_date: date | None
    last_settlement_date: date | None
    eic: str
    name: str
    last_success_at: datetime | None
    last_error: str
    pending_jobs: int
    archived_days: int


NEVER_CONNECTED = EpiasStatus(
    EpiasState.NEVER_CONNECTED,
    False,
    None,
    None,
    None,
    0,
    None,
    None,
    None,
    None,
    "",
    "",
    None,
    "",
    0,
    0,
)


def get_epias_status(plant_id: int, *, actor: User) -> EpiasStatus:
    plant = member_plant(plant_id, actor)
    connection = EpiasConnection.objects.filter(plant=plant).first()
    return epias_connection_status(connection)


def _health(
    connection: EpiasConnection, jobs: QuerySet[EpiasCollectionJob], pending: int
) -> tuple[EpiasHealth | None, int]:
    """The newest nightly batch decides once nothing waits for a worker."""
    if not connection.enabled:
        return None, 0
    batch = jobs.aggregate(scheduled=Max("scheduled_for"))["scheduled"]
    if pending or batch is None:
        return EpiasHealth.COLLECTING, 0
    outcome = jobs.filter(scheduled_for=batch).aggregate(
        failed=Count("target_date", filter=Q(status=EpiasJobStatus.FAILED), distinct=True),
        waiting=Count("pk", filter=Q(status=EpiasJobStatus.AWAITING_PUBLICATION)),
    )
    if outcome["failed"]:
        return EpiasHealth.NEEDS_ATTENTION, outcome["failed"]
    if outcome["waiting"]:
        return EpiasHealth.WAITING_FOR_PUBLICATION, 0
    return EpiasHealth.UP_TO_DATE, 0


def epias_connection_status(connection: EpiasConnection | None) -> EpiasStatus:
    if connection is None:
        return NEVER_CONNECTED
    jobs = EpiasCollectionJob.objects.filter(connection=connection)
    snapshots = EpiasSnapshot.objects.filter(_source_identity(connection), connection=connection)
    populated = snapshots.exclude(readings=[])
    dates = populated.aggregate(
        first=Min("target_date", filter=Q(source_kind=EpiasSourceKind.SETTLEMENT_INJECTION)),
        last=Max("target_date", filter=Q(source_kind=EpiasSourceKind.SETTLEMENT_INJECTION)),
        complete_through=Max(
            "target_date",
            filter=Q(source_kind=EpiasSourceKind.REALTIME_GENERATION, complete=True),
        ),
    )
    latest = populated.order_by("-available_at", "-pk").first()
    last_job = jobs.order_by("-scheduled_for", "-pk").first()
    pending = jobs.filter(status__in=(EpiasJobStatus.PENDING, EpiasJobStatus.RUNNING)).count()
    health, failed_days = _health(connection, jobs, pending)
    return EpiasStatus(
        state=EpiasState.CONNECTED if connection.enabled else EpiasState.DISCONNECTED,
        connected=connection.enabled,
        disconnected_at=None if connection.enabled else connection.updated_at,
        health=health,
        complete_through=dates["complete_through"],
        failed_days=failed_days,
        provider_plant_id=connection.provider_plant_id,
        settlement_plant_id=connection.settlement_plant_id,
        first_settlement_date=dates["first"],
        last_settlement_date=dates["last"],
        eic=connection.eic,
        name=connection.name,
        last_success_at=latest.available_at if latest else None,
        last_error=last_job.last_error if last_job else "",
        pending_jobs=pending,
        archived_days=populated.values("target_date").distinct().count(),
    )


def _source_identity(connection: EpiasConnection) -> Q:
    sources = Q(
        source_kind=EpiasSourceKind.REALTIME_GENERATION,
        job__provider_plant__id=connection.provider_plant_id,
    )
    if connection.settlement_plant_id is not None:
        sources |= Q(
            source_kind=EpiasSourceKind.SETTLEMENT_INJECTION,
            job__provider_plant__id=connection.settlement_plant_id,
        )
    return sources & Q(job__provider_plant__eic=connection.eic)
