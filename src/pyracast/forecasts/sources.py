from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from django.db.models import Q, QuerySet

from pyracast.connections.contracts import ProviderData
from pyracast.errors import BusyError, ErrorCode
from pyracast.forecasts.models import CollectionJobStatus, ForecastSnapshot
from pyracast.forecasts.selection import ForecastOption
from pyracast.intervals import NormalizedInterval
from pyracast.irradiance import SourceKind


@dataclass(frozen=True)
class LaterFetches:
    """Fetches made after a day's sunrise, with the moment each became known."""

    options: tuple[ForecastOption, ...] = ()
    known_at: Mapping[int, datetime] = field(default_factory=dict)


def _known_forecasts(
    binding_id: int, start: datetime, end: datetime, as_of: datetime
) -> QuerySet[ForecastSnapshot]:
    return (
        ForecastSnapshot.objects.filter(
            binding_id=binding_id,
            job__status=CollectionJobStatus.SUCCEEDED,
            source_kind="forecast",
            coverage_start__lt=end,
            coverage_end__gt=start,
            collected_at__lte=as_of,
            available_at__lte=as_of,
            forecastresult__published_at__lte=as_of,
        )
        .filter(Q(issued_at__isnull=True) | Q(issued_at__lte=as_of))
        .order_by("-collected_at", "-available_at", "-pk")
    )


def current_forecast_snapshots(
    *, binding_id: int, start: datetime, end: datetime, as_of: datetime
) -> list[ForecastSnapshot]:
    return covering_snapshots(
        _known_forecasts(binding_id, start, end, as_of).select_related("job"), start, end
    )


def later_fetches(
    binding_id: int, start: datetime, end: datetime, sunrise: datetime, now: datetime
) -> LaterFetches:
    rows = list(
        _known_forecasts(binding_id, start, end, now)
        .filter(collected_at__gte=sunrise)
        .values_list("pk", "collected_at", "forecastresult__published_at")
    )
    return LaterFetches(
        tuple(ForecastOption(pk, collected_at) for pk, collected_at, _ in rows),
        {pk: published_at for pk, _, published_at in rows},
    )


def covering_ids(
    coverages: Iterable[tuple[int, datetime | None, datetime | None]],
    start: datetime,
    end: datetime,
) -> list[int]:
    """Identifiers, in the given order, of the coverages that each still cover part of the day."""
    remaining = [(start, end)]
    identifiers = []
    for identifier, coverage_start, coverage_end in coverages:
        left, right = cast(datetime, coverage_start), cast(datetime, coverage_end)
        if not any(left < stop and right > begin for begin, stop in remaining):
            continue
        identifiers.append(identifier)
        remaining = [
            piece
            for begin, stop in remaining
            for piece in ((begin, min(stop, left)), (max(begin, right), stop))
            if piece[0] < piece[1]
        ]
        if not remaining:
            break
    return identifiers


def covering_snapshots(
    snapshots: QuerySet[ForecastSnapshot], start: datetime, end: datetime
) -> list[ForecastSnapshot]:
    identifiers = covering_ids(
        snapshots.values_list("pk", "coverage_start", "coverage_end"), start, end
    )
    selected = list(snapshots.filter(pk__in=identifiers))
    if {snapshot.pk for snapshot in selected} != set(identifiers):
        raise BusyError(ErrorCode.FORECAST_SOURCES_CHANGED)
    return selected


def snapshot_data(
    snapshot: ForecastSnapshot, *, intervals: Iterable[Mapping[str, object]] | None = None
) -> ProviderData:
    return ProviderData(
        raw_payload=snapshot.raw_payload,
        intervals=NormalizedInterval.from_rows(
            snapshot.normalized_intervals if intervals is None else intervals
        ),
        issued_at=snapshot.issued_at,
        collected_at=snapshot.collected_at,
        source_kind=cast(SourceKind, snapshot.source_kind),
    )
