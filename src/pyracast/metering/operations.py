from datetime import date, timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Min, Q
from django.utils import timezone

from pyracast.accounts.operations import require_admin, require_member
from pyracast.epias import EpiasClient
from pyracast.metering.client import get_epias_client
from pyracast.metering.models import (
    EpiasCollectionJob,
    EpiasConnection,
    EpiasJobStatus,
    EpiasSnapshot,
    EpiasSourceKind,
)
from pyracast.plants.models import Plant, PlantEvent, PlantEventAction
from pyracast.plants.operations import configuration_is_current
from pyracast.regions.models import Region


def member_plant(plant_id: int, actor: User) -> Plant:
    plant = Plant.objects.select_related("region").get(pk=plant_id, region__deleted_at__isnull=True)
    require_member(actor, plant.region.tenant_id)
    return plant


def connect_epias(
    plant_id: int,
    *,
    actor: User,
    provider_plant_id: int,
    expected_eic: str,
    client: EpiasClient | None = None,
) -> EpiasConnection:
    require_admin(actor)
    plant = Plant.objects.select_related("region").get(pk=plant_id, region__deleted_at__isnull=True)
    if isinstance(provider_plant_id, bool) or provider_plant_id <= 0 or not expected_eic.strip():
        raise ValidationError("invalid_epias_plant")
    if plant.current_configuration is None or not configuration_is_current(
        plant.current_configuration
    ):
        raise ValidationError("configuration_unavailable")
    provider = client or get_epias_client()
    candidates = provider.list_plants()
    selected = next((p for p in candidates if p.id == provider_plant_id), None)
    if selected is None or selected.eic != expected_eic.strip():
        raise ValidationError("epias_plant_identity_mismatch")
    settlement = provider.resolve_settlement_plant(selected.eic)
    with transaction.atomic():
        Region.objects.select_for_update().get(pk=plant.region_id)
        plant = Plant.objects.select_for_update().get(pk=plant.pk)
        require_admin(User.objects.select_for_update().get(pk=actor.pk))
        if (
            plant.region.deleted_at is not None
            or plant.current_configuration is None
            or not configuration_is_current(plant.current_configuration)
        ):
            raise ValidationError("configuration_unavailable")
        connection = EpiasConnection.objects.select_for_update().filter(plant=plant).first()
        if connection is None:
            connection = EpiasConnection(plant=plant, connected_by=actor)
        else:
            connection.revision += 1
            EpiasCollectionJob.objects.filter(
                connection=connection,
                status__in=(EpiasJobStatus.PENDING, EpiasJobStatus.RUNNING),
            ).update(status=EpiasJobStatus.SUPERSEDED, lease_token=None, lease_expires_at=None)
        connection.provider_plant_id = selected.id
        connection.settlement_plant_id = settlement.id if settlement else None
        connection.settlement_checked_at = timezone.now()
        connection.eic = selected.eic
        connection.name = selected.name
        connection.connected_by = actor
        connection.enabled = True
        connection.save()
        PlantEvent.objects.create(
            plant=plant,
            actor=actor,
            action=PlantEventAction.EPIAS_CONNECTED,
            details={
                "provider_plant_id": connection.provider_plant_id,
                "eic": connection.eic,
                "revision": connection.revision,
            },
        )
        from pyracast.metering.scheduler import enqueue_connection

        enqueue_connection(connection, timezone.now(), initial=True)
        return connection


def disconnect_epias(plant_id: int, *, actor: User) -> None:
    require_admin(actor)
    plant = Plant.objects.select_related("region").get(pk=plant_id, region__deleted_at__isnull=True)
    with transaction.atomic():
        Region.objects.select_for_update().get(pk=plant.region_id)
        Plant.objects.select_for_update().get(pk=plant.pk)
        require_admin(User.objects.select_for_update().get(pk=actor.pk))
        connection = EpiasConnection.objects.select_for_update().filter(plant=plant).first()
        if connection is None:
            return
        connection.enabled = False
        connection.save(update_fields=("enabled", "updated_at"))
        PlantEvent.objects.create(
            plant=plant,
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


def get_epias_status(plant_id: int, *, actor: User) -> dict[str, object]:
    plant = member_plant(plant_id, actor)
    connection = EpiasConnection.objects.filter(plant=plant).first()
    return epias_connection_status(connection)


def epias_connection_status(connection: EpiasConnection | None) -> dict[str, object]:
    if connection is None:
        return {
            "connected": False,
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
    jobs = EpiasCollectionJob.objects.filter(connection=connection)
    snapshots = EpiasSnapshot.objects.filter(_source_identity(connection), connection=connection)
    populated = snapshots.exclude(readings=[])
    settlement_dates = populated.filter(source_kind=EpiasSourceKind.SETTLEMENT_INJECTION).aggregate(
        first=Min("target_day"), last=Max("target_day")
    )
    latest = populated.order_by("-available_at", "-pk").first()
    last_job = jobs.order_by("-scheduled_for", "-pk").first()
    return {
        "connected": connection.enabled,
        "provider_plant_id": connection.provider_plant_id,
        "settlement_plant_id": connection.settlement_plant_id,
        "first_settlement_date": settlement_dates["first"],
        "last_settlement_date": settlement_dates["last"],
        "eic": connection.eic,
        "name": connection.name,
        "last_success_at": latest.available_at if latest else None,
        "last_error": last_job.last_error if last_job else "",
        "pending_jobs": jobs.filter(
            status__in=(EpiasJobStatus.PENDING, EpiasJobStatus.RUNNING)
        ).count(),
        "archived_days": populated.values("target_day").distinct().count(),
    }


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


def read_epias_history(
    plant_id: int,
    *,
    actor: User,
    start_day: date | None = None,
    end_day: date | None = None,
    source_kind: str | None = EpiasSourceKind.REALTIME_GENERATION,
) -> list[EpiasSnapshot]:
    plant = member_plant(plant_id, actor)
    end_day = end_day or timezone.now().date()
    start_day = start_day or end_day - timedelta(days=30)
    if end_day < start_day or (end_day - start_day).days > 366:
        raise ValidationError("invalid_history_window")
    if source_kind is not None and source_kind not in EpiasSourceKind.values:
        raise ValidationError("invalid_production_source")
    connection = EpiasConnection.objects.filter(plant=plant).first()
    if connection is None:
        return []
    snapshots = EpiasSnapshot.objects.filter(
        _source_identity(connection),
        connection=connection,
        target_day__range=(start_day, end_day),
    )
    if source_kind is not None:
        snapshots = snapshots.filter(source_kind=source_kind)
    return list(
        snapshots.order_by(
            "target_day", "source_kind", "-collected_at", "-available_at", "-pk"
        ).distinct("target_day", "source_kind")
    )
