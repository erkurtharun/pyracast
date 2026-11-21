from collections.abc import Sequence
from datetime import date, datetime

from pyracast.incidents.evidence import select_incidents
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


def runs_with_unchanged_incidents(
    configuration: PlantConfiguration,
    items: Sequence[tuple[date, CalibrationRun]],
    known_at: datetime,
) -> list[tuple[date, CalibrationRun]]:
    """The items whose run passes `incidents_unchanged`, reading the evidence once for the
    widest window at the same knowledge time."""
    if not items:
        return []
    windows = [run_window(run) for _, run in items]
    evidence = select_incidents(
        configuration.plant_id,
        min(start for start, _ in windows),
        max(end for _, end in windows),
        known_at,
    )
    return [
        (item, run)
        for (item, run), (start, end) in zip(items, windows, strict=True)
        if sorted(run.incident_revision_ids)
        == sorted(
            row.revision_id
            for row in evidence
            if row.starts_at < end and (row.ends_at is None or row.ends_at > start)
        )
    ]
