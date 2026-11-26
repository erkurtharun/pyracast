from collections.abc import Sequence
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import Max, Min, Q

from pyracast.forecasts.actuals import select_archived_actual_source
from pyracast.forecasts.models import CollectionJobStatus, ForecastSnapshot
from pyracast.forecasts.sources import snapshot_rows
from pyracast.plants.models import PlantConfiguration
from pyracast.plants.numerics import PowerInterval
from pyracast.plants.transfer import reference_power_mw, transfer_model


def reference_intervals(
    configuration: PlantConfiguration, day: date, as_of: datetime
) -> tuple[PowerInterval, ...]:
    source = select_archived_actual_source(configuration.binding, day, as_of)
    if source is None:
        return ()
    return actual_power_intervals(
        source.snapshot,
        configuration,
        model_version=transfer_model(configuration, None),
        source_intervals=source.intervals,
    )


def actual_power_intervals(
    snapshot: ForecastSnapshot,
    configuration: PlantConfiguration,
    *,
    model_version: str,
    source_intervals: Sequence[dict[str, Any]] | None = None,
) -> tuple[PowerInterval, ...]:
    result = []
    for row in snapshot_rows(snapshot) if source_intervals is None else source_intervals:
        power = row.get("power_kw")
        if power is None or power < 0:
            continue
        start = datetime.fromisoformat(row["start"])
        end = datetime.fromisoformat(row["end"])
        result.append(
            PowerInterval(
                start,
                end,
                reference_power_mw(configuration, start, end, power, model_version=model_version),
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
            job__status=CollectionJobStatus.SUCCEEDED,
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
