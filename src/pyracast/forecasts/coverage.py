import math
from collections.abc import Iterable
from datetime import date, datetime

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds


def history_day_complete(snapshot: ForecastSnapshot, target: date, timezone: str) -> bool:
    return intervals_cover_day(snapshot.normalized_intervals, target, timezone)


def intervals_cover_day(intervals: Iterable[object], target: date, timezone: str) -> bool:
    start, end = local_day_bounds(target, timezone)
    cursor = start
    rows: list[tuple[datetime, datetime, object]] = []
    for row in intervals:
        if not isinstance(row, dict):
            return False
        raw_start, raw_end = row.get("start"), row.get("end")
        if not isinstance(raw_start, str) or not isinstance(raw_end, str):
            return False
        try:
            left, right = datetime.fromisoformat(raw_start), datetime.fromisoformat(raw_end)
        except ValueError:
            return False
        if left.utcoffset() is None or right.utcoffset() is None:
            return False
        rows.append((left, right, row.get("power_kw")))
    for left, right, power in sorted(rows, key=lambda item: item[0]):
        if right <= start or left >= end:
            continue
        if (
            max(left, start) != cursor
            or right <= left
            or isinstance(power, bool)
            or not isinstance(power, int | float)
            or not math.isfinite(power)
            or power < 0
        ):
            return False
        cursor = min(right, end)
    return cursor >= end
