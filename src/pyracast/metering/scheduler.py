from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from pyracast.forecasts.models import CollectionSchedule
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
    today = now.astimezone(MARKET_ZONE).date()
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
    jobs = []
    for offset in range(1, 8):
        job, _ = EpiasCollectionJob.objects.get_or_create(
            connection=connection,
            connection_revision=connection.revision,
            configuration=configuration,
            target_day=today - timedelta(days=offset),
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
    return jobs


def enqueue_due_epias_jobs(*, now: datetime | None = None) -> list[int]:
    now = now or timezone.now()
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    identifiers = []
    for connection in EpiasConnection.objects.filter(enabled=True).select_related("plant"):
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
                identifiers.extend(enqueue_connection(current, now))
    return identifiers
