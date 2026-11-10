from datetime import date, datetime, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from django.db.models import Max, Min, Q

from pyracast.forecasts.history import select_actual_snapshot
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.plants.models import PlantConfiguration
from pyracast.plants.numerics import PowerInterval


def reference_intervals(
    configuration: PlantConfiguration, day: date, as_of: datetime
) -> tuple[PowerInterval, ...]:
    snapshot = select_actual_snapshot(configuration.binding, day, as_of)
    if snapshot is None:
        return ()
    return actual_power_intervals(snapshot, configuration)


def actual_power_intervals(
    snapshot: ForecastSnapshot, configuration: PlantConfiguration
) -> tuple[PowerInterval, ...]:
    result = []
    for row in snapshot.normalized_intervals:
        power = row.get("power_kw")
        if (
            isinstance(power, bool)
            or not isinstance(power, int | float)
            or not isfinite(power)
            or power < 0
        ):
            continue
        start = datetime.fromisoformat(row["start"])
        end = datetime.fromisoformat(row["end"])
        if start.utcoffset() is None or end.utcoffset() is None or end <= start:
            continue
        result.append(
            PowerInterval(
                start,
                end,
                power / configuration.binding.capacity_kw * configuration.dc_capacity_mw,
            )
        )
    return tuple(sorted(result, key=lambda interval: interval.start))


def interval_is_covered(
    intervals: tuple[PowerInterval, ...], start: datetime, end: datetime
) -> bool:
    cursor = start
    for piece in intervals:
        if piece.end <= start or piece.start >= end:
            continue
        left, right = max(start, piece.start), min(end, piece.end)
        if (
            left != cursor
            or piece.power_mw is None
            or not isfinite(piece.power_mw)
            or piece.power_mw < 0
        ):
            return False
        cursor = right
    return cursor == end


def available_date_bounds(
    configuration: PlantConfiguration, as_of: datetime
) -> tuple[date | None, date | None]:
    bounds = (
        ForecastSnapshot.objects.filter(
            job__binding_id=configuration.binding_id,
            job__status="succeeded",
            source_kind="estimated_actuals",
            collected_at__lte=as_of,
            available_at__lte=as_of,
        )
        .filter(Q(issued_at__isnull=True) | Q(issued_at__lte=as_of))
        .aggregate(start=Min("coverage_start"), end=Max("coverage_end"))
    )
    if bounds["start"] is None or bounds["end"] is None:
        return None, None
    zone = ZoneInfo(configuration.binding.timezone)
    first = bounds["start"].astimezone(zone).date()
    last = min(
        (bounds["end"] - timedelta(microseconds=1)).astimezone(zone).date(),
        as_of.astimezone(zone).date() - timedelta(days=1),
    )
    return (first, last) if first <= last else (None, None)
