from datetime import datetime

from django.utils import timezone

from pyracast.incidents.evidence import select_incidents
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import CalibrationRun, CalibrationStatus, PlantConfiguration


def incident_ids(
    configuration: PlantConfiguration, start: datetime, end: datetime, known_at: datetime
) -> list[int]:
    return sorted(
        row.revision_id for row in select_incidents(configuration.plant_id, start, end, known_at)
    )


def incidents_unchanged(run: CalibrationRun, known_at: datetime) -> bool:
    start, _ = local_day_bounds(run.window_start, run.binding.timezone)
    _, end = local_day_bounds(run.window_end, run.binding.timezone)
    return sorted(run.incident_revision_ids) == incident_ids(
        run.configuration, start, end, known_at
    )


def incidents_changed(plant_id: int) -> None:
    moment = timezone.now()
    for run in CalibrationRun.objects.filter(
        plant_id=plant_id, status__in=(CalibrationStatus.PENDING, CalibrationStatus.RUNNING)
    ).select_related("configuration", "binding"):
        if not incidents_unchanged(run, moment):
            run.status = CalibrationStatus.SUPERSEDED
            run.completed_at = moment
            run.lease_token = None
            run.lease_expires_at = None
            run.save(update_fields=("status", "completed_at", "lease_token", "lease_expires_at"))
    from pyracast.plants.preparation import request_preparation

    request_preparation([plant_id], moment)
