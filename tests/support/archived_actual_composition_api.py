from datetime import date, timedelta

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from tests.support.domain import System
from tests.support.plant_operations import half_hours, snapshot_for

DAY = date(2026, 11, 8)


def acquired_actuals(
    system: System, first_hour: int, last_hour: int, power_kw: float
) -> ForecastSnapshot:
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    acquired = start + timedelta(hours=last_hour, minutes=5)
    snapshot = snapshot_for(
        system,
        DAY,
        collected_at=acquired,
        source_kind="estimated_actuals",
        normalized_intervals=half_hours(
            start + timedelta(hours=first_hour), start + timedelta(hours=last_hour), power_kw
        ),
        available_at=acquired,
    )
    snapshot.job.endpoint, snapshot.job.status = "estimated_actuals", "succeeded"
    snapshot.job.save(update_fields=("endpoint", "status"))
    return snapshot
