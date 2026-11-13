from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any


@dataclass(frozen=True, slots=True)
class ForecastVintage:
    snapshot_id: int
    starts_at: datetime
    ends_at: datetime
    intervals: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ForecastComposition:
    intervals: tuple[dict[str, Any], ...]
    segments: list[dict[str, object]]
    snapshot_ids: tuple[int, ...]
    prorated: bool


@dataclass(frozen=True, slots=True)
class _NativeInterval:
    row: dict[str, Any]
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class _Vintage:
    snapshot_id: int
    start: datetime
    end: datetime
    intervals: tuple[_NativeInterval, ...]


def _bounds(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("Forecast timestamps must be timezone-aware")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end <= start:
        raise ValueError("Forecast intervals must have positive duration")
    return start, end


def _validate(vintage: ForecastVintage) -> _Vintage:
    start, end = _bounds(vintage.starts_at, vintage.ends_at)
    intervals = []
    for row in vintage.intervals:
        row_start, row_end = row.get("start"), row.get("end")
        if not isinstance(row_start, str) or not isinstance(row_end, str):
            raise ValueError("Invalid source interval timestamps")
        begin, stop = _bounds(datetime.fromisoformat(row_start), datetime.fromisoformat(row_end))
        if begin < start or stop > end:
            raise ValueError("Source intervals must lie within forecast coverage")
        intervals.append(_NativeInterval(row, begin, stop))
    intervals.sort(key=lambda interval: interval.start)
    if any(left.end > right.start for left, right in pairwise(intervals)):
        raise ValueError("Source intervals must not overlap")
    return _Vintage(vintage.snapshot_id, start, end, tuple(intervals))


def _ownership(
    vintages: tuple[_Vintage, ...], start: datetime, end: datetime
) -> list[tuple[int, datetime, datetime]]:
    boundaries = {start, end}
    for vintage in vintages:
        if vintage.start < end and vintage.end > start:
            boundaries.update((max(start, vintage.start), min(end, vintage.end)))
    ordered = sorted(boundaries)
    segments: list[tuple[int, datetime, datetime]] = []
    for begin, stop in pairwise(ordered):
        for index, vintage in enumerate(vintages):
            if vintage.start <= begin and vintage.end >= stop:
                if segments and segments[-1][0] == index and segments[-1][2] == begin:
                    segments[-1] = (index, segments[-1][1], stop)
                else:
                    segments.append((index, begin, stop))
                break
    return segments


def compose_forecasts(
    vintages: Sequence[ForecastVintage], start: datetime, end: datetime
) -> ForecastComposition:
    """Select coverage in caller priority order, keeping native means and missingness."""
    start, end = _bounds(start, end)
    sources = tuple(_validate(vintage) for vintage in vintages)
    ownership = _ownership(sources, start, end)
    intervals: list[dict[str, Any]] = []
    segments: list[dict[str, object]] = []
    used: set[int] = set()
    prorated = False
    for index, begin, stop in ownership:
        vintage = sources[index]
        used.add(index)
        segments.append(
            {
                "start": begin.isoformat(),
                "end": stop.isoformat(),
                "snapshot_id": vintage.snapshot_id,
            }
        )
        for native in vintage.intervals:
            piece_start, piece_end = max(begin, native.start), min(stop, native.end)
            if piece_start >= piece_end:
                continue
            row = {**native.row, "source_snapshot_id": vintage.snapshot_id}
            if piece_start != native.start or piece_end != native.end:
                # Native means represent clipped durations; no subinterval profile is inferred.
                row.update(
                    start=piece_start.isoformat(),
                    end=piece_end.isoformat(),
                    original_start=native.row["start"],
                    original_end=native.row["end"],
                )
                prorated = True
            intervals.append(row)
    return ForecastComposition(
        tuple(intervals),
        segments,
        tuple(vintage.snapshot_id for index, vintage in enumerate(sources) if index in used),
        prorated,
    )
