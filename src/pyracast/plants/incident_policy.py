from datetime import datetime

from pyracast.incidents.evidence import select_incidents
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import CalibrationRun, PlantConfiguration


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
