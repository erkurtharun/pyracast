from collections.abc import Iterable, Mapping
from datetime import date
from operator import attrgetter

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.forecasts.sources import snapshot_rows
from pyracast.intervals import NormalizedInterval
from pyracast.irradiance import local_day_bounds


def history_day_complete(snapshot: ForecastSnapshot, target: date, timezone: str) -> bool:
    return intervals_cover_day(snapshot_rows(snapshot), target, timezone)


def intervals_cover_day(
    intervals: Iterable[Mapping[str, object]], target: date, timezone: str
) -> bool:
    start, end = local_day_bounds(target, timezone)
    cursor = start
    for row in sorted(NormalizedInterval.from_rows(intervals), key=attrgetter("start")):
        if row.end <= start or row.start >= end:
            continue
        if max(row.start, start) != cursor or row.power_kw is None or row.power_kw < 0:
            return False
        cursor = min(row.end, end)
    return cursor >= end
