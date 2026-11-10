from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from pyracast.epias import EpiasError
from pyracast.forecasts.models import CollectionSchedule
from pyracast.metering.client import get_epias_client
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection
from pyracast.notifications.schedule import local_slot
from pyracast.plants.models import Plant
from pyracast.plants.operations import configuration_is_current
from pyracast.regions.models import Region

MARKET_ZONE = ZoneInfo("Europe/Istanbul")


def enqueue_connection(
    connection: EpiasConnection, now: datetime, *, initial: bool = False
) -> list[int]:
    plant = connection.plant
    configuration = plant.current_configuration
    if (
        not connection.enabled
        or configuration is None
        or not configuration_is_current(configuration)
    ):
        return []
    scheduled_for = now
    if not initial:
        schedule = CollectionSchedule.objects.filter(region=plant.region).first()
        if schedule is not None and (not schedule.enabled or not schedule.times):
            return []
        clock = (
            min(schedule.times) if schedule else settings.PYRACAST_CUTOFF_LOCAL.strftime("%H:%M")
        )
        slot = local_slot(
            now.astimezone(ZoneInfo(plant.region.timezone)).date(),
            clock,
            ZoneInfo(plant.region.timezone),
        )
        if slot is None or slot > now or (schedule and slot < schedule.effective_from):
            return []
        if slot < connection.updated_at:
            return []
        scheduled_for = slot
    today = scheduled_for.astimezone(MARKET_ZONE).date()
    jobs = []
    for offset in range(1, 8):
        job, _ = EpiasCollectionJob.objects.get_or_create(
            connection=connection,
            connection_revision=connection.revision,
            configuration=configuration,
            target_day=today - timedelta(days=offset),
            source_kind="realtime_generation",
            scheduled_for=scheduled_for,
            defaults={
                "provider_plant": {
                    "id": connection.provider_plant_id,
                    "eic": connection.eic,
                    "name": connection.name,
                    "short_name": "",
                },
                "next_attempt_at": now,
            },
        )
        jobs.append(job.pk)
    if connection.settlement_plant_id is not None:
        month_end = today.replace(day=1) - timedelta(days=1)
        initial_settlement = not (
            EpiasCollectionJob.objects.filter(
                connection=connection,
                connection_revision=connection.revision,
                source_kind="settlement_injection",
            )
            .exclude(scheduled_for=scheduled_for)
            .exists()
        )
        for _offset in range(3 if initial or initial_settlement or today.weekday() == 0 else 2):
            month_start = month_end.replace(day=1)
            job, _ = EpiasCollectionJob.objects.get_or_create(
                connection=connection,
                connection_revision=connection.revision,
                configuration=configuration,
                source_kind="settlement_injection",
                target_day=month_start,
                scheduled_for=scheduled_for,
                defaults={
                    "period_end_day": month_end,
                    "provider_plant": {
                        "id": connection.settlement_plant_id,
                        "eic": connection.eic,
                        "name": connection.name,
                        "short_name": "",
                    },
                    "next_attempt_at": now,
                },
            )
            jobs.append(job.pk)
            month_end = month_start - timedelta(days=1)
    return jobs


def enqueue_due_epias_jobs(*, now: datetime | None = None) -> list[int]:
    now = now or timezone.now()
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    identifiers = []
    for connection in EpiasConnection.objects.filter(
        enabled=True,
        plant__deleted_at__isnull=True,
        plant__region__deleted_at__isnull=True,
    ).select_related("plant"):
        check_settlement = (
            connection.settlement_plant_id is None
            and (
                connection.settlement_checked_at is None
                or connection.settlement_checked_at <= now - timedelta(days=1)
            )
            and connection.plant.current_configuration is not None
            and configuration_is_current(connection.plant.current_configuration)
        )
        settlement_id = _resolve_settlement_id(connection) if check_settlement else None
        with transaction.atomic():
            region = (
                Region.objects.select_for_update().filter(pk=connection.plant.region_id).first()
            )
            if region is None:
                continue
            plant = Plant.objects.select_for_update().filter(pk=connection.plant_id).first()
            if plant is None:
                continue
            current = EpiasConnection.objects.select_for_update().filter(pk=connection.pk).first()
            if current is not None:
                if (
                    check_settlement
                    and current.revision == connection.revision
                    and current.eic == connection.eic
                    and current.settlement_plant_id is None
                ):
                    current.settlement_plant_id = settlement_id
                    current.settlement_checked_at = now
                    current.save(update_fields=("settlement_plant_id", "settlement_checked_at"))
                identifiers.extend(enqueue_connection(current, now))
    return identifiers


def _resolve_settlement_id(connection: EpiasConnection) -> int | None:
    try:
        selected = get_epias_client().resolve_settlement_plant(connection.eic)
    except EpiasError:
        return None
    return selected.id if selected else None
