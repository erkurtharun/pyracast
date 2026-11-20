from collections.abc import Iterable
from datetime import datetime

from pyracast.incidents.evidence import IncidentEvidence, select_incidents
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import CalibrationRun, PlantConfiguration


def incident_ids(
    configuration: PlantConfiguration, start: datetime, end: datetime, known_at: datetime
) -> list[int]:
    return sorted(
        row.revision_id for row in select_incidents(configuration.plant_id, start, end, known_at)
    )


def run_window(run: CalibrationRun) -> tuple[datetime, datetime]:
    start, _ = local_day_bounds(run.window_start, run.binding.timezone)
    _, end = local_day_bounds(run.window_end, run.binding.timezone)
    return start, end


def incidents_unchanged(run: CalibrationRun, known_at: datetime) -> bool:
    start, end = run_window(run)
    return sorted(run.incident_revision_ids) == incident_ids(
        run.configuration, start, end, known_at
    )


def incidents_unchanged_within(run: CalibrationRun, evidence: Iterable[IncidentEvidence]) -> bool:
    """The same comparison as `incidents_unchanged` over evidence selected once for a wider
    window at the same knowledge time."""
    start, end = run_window(run)
    return sorted(run.incident_revision_ids) == sorted(
        row.revision_id
        for row in evidence
        if row.starts_at < end and (row.ends_at is None or row.ends_at > start)
    )
