from datetime import date, timedelta

from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import (
    Plant,
)
from pyracast.plants.production_changes import ObservationInput, import_observations
from pyracast.solar import daylight_window
from tests.support.domain import System
from tests.support.plant_training import actual_snapshot_for


def add_days(
    system: System,
    plant: Plant,
    first: int,
    count: int,
    *,
    invalid: bool = False,
    daylight_only: bool = False,
) -> None:
    assert plant.current_configuration_id is not None
    rows = []
    for offset in range(first, first + count):
        day = date(2026, 11, offset)
        actual_snapshot_for(system, day)
        start, end = local_day_bounds(day, system.binding.timezone)
        cursor = start
        windows = daylight_window(
            day, system.binding.timezone, system.binding.latitude, system.binding.longitude
        ).windows
        while cursor < end:
            duration = timedelta(minutes=30 if offset % 2 else 60)
            selected = any(cursor < stop and cursor + duration > begin for begin, stop in windows)
            if daylight_only and not selected:
                cursor += duration
                continue
            rows.append(
                ObservationInput(
                    cursor,
                    cursor + duration,
                    26 * duration.total_seconds() / 3600,
                    "invalid" if invalid and cursor == start + timedelta(hours=12) else "valid",
                )
            )
            cursor += duration
    import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="MWh",
        observations=rows,
    )
