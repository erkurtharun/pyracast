from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite
from typing import Any, cast

from django.db.models import Q, QuerySet

from pyracast.connections.contracts import ProviderData
from pyracast.errors import BusyError, ErrorCode
from pyracast.forecasts.faults import decoding, snapshot_row
from pyracast.forecasts.models import CollectionJobStatus, ForecastSnapshot
from pyracast.forecasts.selection import ForecastOption
from pyracast.intervals import NormalizedInterval
from pyracast.irradiance import SourceKind


@dataclass(frozen=True)
class LaterFetch:
    forecast_id: int
    collected_at: datetime
    published_at: datetime


@dataclass(frozen=True)
class LaterFetches:
    """Fetches of a day collected or published from its sunrise on: no forecast saved before it
    holds them."""

    fetches: Mapping[int, LaterFetch] = field(default_factory=dict)

    @property
    def options(self) -> tuple[ForecastOption, ...]:
        return tuple(
            ForecastOption(fetch.forecast_id, fetch.collected_at) for fetch in self.fetches.values()
        )


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
    *,
    binding_id: int,
    start: datetime,
    end: datetime,
    as_of: datetime,
    pinned: LaterFetch | None = None,
) -> list[ForecastSnapshot]:
    """`pinned` answers as that fetch: only it and fetches collected before it take part."""
    snapshots = _known_forecasts(binding_id, start, end, as_of).select_related("job")
    if pinned is not None:
        snapshots = snapshots.filter(
            Q(collected_at__lt=pinned.collected_at) | Q(pk=pinned.forecast_id)
        )
    return covering_snapshots(snapshots, start, end)


def later_fetches(
    binding_id: int, start: datetime, end: datetime, sunrise: datetime, now: datetime
) -> LaterFetches:
    rows = (
        _known_forecasts(binding_id, start, end, now)
        .filter(Q(collected_at__gte=sunrise) | Q(forecastresult__published_at__gte=sunrise))
        .values_list("pk", "collected_at", "forecastresult__published_at")
    )
    return LaterFetches({row[0]: LaterFetch(*row) for row in rows})


def supplying_fetches(result: object) -> tuple[ForecastOption, ...]:
    """The forecast fetches whose figures an answer's result shows, as its provenance names them;
    none when a saved provenance cannot be read."""
    provenance = result.get("provenance") if isinstance(result, Mapping) else None
    if not isinstance(provenance, Mapping):
        return ()
    try:
        return tuple(
            _supplier(source)
            for source in provenance.get("source_snapshots", [provenance])
            if source.get("source_kind", "forecast") == "forecast"
        )
    except AttributeError, KeyError, OverflowError, TypeError, ValueError:
        return ()


def _supplier(source: Mapping[str, object]) -> ForecastOption:
    identifier, collected_at = source["snapshot_id"], source["collected_at"]
    if not isinstance(identifier, int) or not isinstance(collected_at, str):
        raise TypeError("A supplying fetch needs an integer id and a collection time")
    return ForecastOption(identifier, datetime.fromisoformat(collected_at).astimezone(UTC))


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


def snapshot_rows(snapshot: ForecastSnapshot) -> tuple[dict[str, Any], ...]:
    """The stored interval rows of a snapshot, every one an interval of positive duration with
    aware bounds and a power a float represents or none."""
    with decoding("forecast_snapshot", snapshot.pk):
        rows = snapshot.normalized_intervals
        if not isinstance(rows, list):
            raise TypeError("Stored intervals are not an array.")
        for row in rows:
            _decoded_row(NormalizedInterval.from_row(row))
        return tuple(rows)


def _decoded_row(interval: NormalizedInterval) -> None:
    if interval.end <= interval.start:
        raise ValueError("A stored interval has no duration.")
    if interval.power_kw is not None and not isfinite(float(interval.power_kw)):
        raise ValueError("A stored power is not finite.")


def stored_intervals(
    snapshot: ForecastSnapshot, rows: Iterable[object] | None = None
) -> tuple[NormalizedInterval, ...]:
    with decoding("forecast_snapshot", snapshot.pk):
        source = snapshot_rows(snapshot) if rows is None else rows
        return tuple(_stored_interval(row, snapshot.pk) for row in source)


def _stored_interval(row: object, snapshot_id: int) -> NormalizedInterval:
    with snapshot_row(row, snapshot_id):
        return NormalizedInterval.from_row(row)


def snapshot_data(
    snapshot: ForecastSnapshot, *, intervals: Iterable[Mapping[str, object]] | None = None
) -> ProviderData:
    return ProviderData(
        raw_payload=snapshot.raw_payload,
        intervals=stored_intervals(snapshot, intervals),
        issued_at=snapshot.issued_at,
        collected_at=snapshot.collected_at,
        source_kind=cast(SourceKind, snapshot.source_kind),
    )
