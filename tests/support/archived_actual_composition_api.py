from datetime import date, timedelta

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from tests.support.domain import System
from tests.support.plant_operations import snapshot_for

DAY = date(2026, 11, 8)


def acquired_actuals(
    system: System, first_hour: int, last_hour: int, power_kw: float
) -> ForecastSnapshot:
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    snapshot = snapshot_for(
        system,
        DAY,
        power_kw=power_kw,
        collected_at=start + timedelta(hours=last_hour, minutes=5),
        source_kind="estimated_actuals",
    )
    snapshot.normalized_intervals = snapshot.normalized_intervals[first_hour * 2 : last_hour * 2]
    snapshot.coverage_start = start + timedelta(hours=first_hour)
    snapshot.coverage_end = start + timedelta(hours=last_hour)
    snapshot.available_at = snapshot.collected_at
    snapshot.save()
    snapshot.job.endpoint, snapshot.job.status = "estimated_actuals", "succeeded"
    snapshot.job.save(update_fields=("endpoint", "status"))
    return snapshot
