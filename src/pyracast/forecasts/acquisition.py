from datetime import UTC, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.intraday import completed_actual_intervals, native_actual_snapshots
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_sources import compose_snapshots
from pyracast.solar import daylight_window

NATIVE_PERIOD = timedelta(minutes=30)


def missing_actuals_window(
    binding: SiteBinding, now: datetime, *, acquired: ForecastSnapshot | None = None
) -> tuple[datetime, datetime] | None:
    now = now.astimezone(UTC)
    end = now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0)
    available_start = now - timedelta(hours=168)
    zone = ZoneInfo(binding.timezone)
    today = now.astimezone(zone).date()
    required: list[tuple[datetime, datetime]] = []
    for offset in reversed(range(8)):
        day = today - timedelta(days=offset)
        start, stop = local_day_bounds(day, binding.timezone)
        light = daylight_window(day, binding.timezone, binding.latitude, binding.longitude)
        windows: list[tuple[datetime, datetime]] = []
        for sunrise, sunset in light.windows:
            left = sunrise.astimezone(zone).replace(minute=0, second=0, microsecond=0)
            right = sunset.astimezone(zone)
            rounded = right.replace(minute=0, second=0, microsecond=0)
            if right != rounded:
                rounded += timedelta(hours=1)
            windows.append((left.astimezone(UTC), rounded.astimezone(UTC)))
        start = start.replace(minute=start.minute // 30 * 30, second=0, microsecond=0)
        while start < stop and start + NATIVE_PERIOD <= end:
            right = start + NATIVE_PERIOD
            if start >= available_start and any(
                start < sunset and right > sunrise for sunrise, sunset in windows
            ):
                required.append((start, right))
            start = right
    if not required:
        return None
    snapshots = native_actual_snapshots(binding, available_start, end, now)
    if (
        acquired is not None
        and acquired.coverage_start is not None
        and acquired.coverage_end is not None
    ):
        snapshots = [acquired, *(snapshot for snapshot in snapshots if snapshot.pk != acquired.pk)]
    try:
        composition = compose_snapshots(snapshots, available_start, end)
        rows = completed_actual_intervals(composition.intervals, now)
    except ValueError, KeyError, TypeError, OverflowError:
        rows = ()
    archived = [
        (
            datetime.fromisoformat(cast(str, row["start"])),
            datetime.fromisoformat(cast(str, row["end"])),
        )
        for row in rows
    ]
    for left, right in required:
        cursor = left
        for start, stop in archived:
            if start <= cursor < stop:
                cursor = stop
            if cursor >= right:
                break
        if cursor < right:
            return left, end
    return None
