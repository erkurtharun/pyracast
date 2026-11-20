from datetime import date, datetime, timedelta

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import (
    Plant,
)
from pyracast.plants.production_changes import ObservationInput, import_observations
from tests.support.domain import System
from tests.support.plant_operations import snapshot_for


def actual_snapshot_for(
    system: System, day: date, *, power_kw: float = 0.4, collected_at: datetime | None = None
) -> ForecastSnapshot:
    _, end = local_day_bounds(day, system.binding.timezone)
    acquired = end + timedelta(hours=1) if collected_at is None else collected_at
    source = snapshot_for(
        system,
        day,
        power_kw=power_kw,
        collected_at=acquired,
        source_kind="estimated_actuals",
        available_at=acquired,
    )
    source.job.endpoint, source.job.status = "estimated_actuals", "succeeded"
    source.job.save()
    return source


def history(system: System, plant: Plant) -> list[int]:
    assert plant.current_configuration_id is not None
    rows: list[ObservationInput] = []
    sources = []
    for offset in range(7):
        source = actual_snapshot_for(system, date(2026, 11, 2) + timedelta(days=offset))
        sources.append(source.pk)
        start = datetime.fromisoformat(source.normalized_intervals[0]["start"])
        rows.extend(
            ObservationInput(start + timedelta(hours=hour), start + timedelta(hours=hour + 1), 26)
            for hour in range(24)
        )
    import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="MWh",
        observations=rows,
    )
    return sources
