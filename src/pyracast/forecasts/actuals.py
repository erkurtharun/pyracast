import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

from django.db.models import Case, IntegerField, Q, QuerySet, Value, When

from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.capacity_normalization import normalization_provenance
from pyracast.forecasts.composition import (
    ForecastComposition,
    ForecastVintage,
    compose_forecasts,
)
from pyracast.forecasts.coverage import history_day_complete
from pyracast.forecasts.intraday import (
    PreparedIntradaySource,
    actual_coverage_complete,
    apply_intraday_source,
    completed_actual_intervals,
    native_actual_queryset,
    native_actual_snapshots,
)
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJobStatus,
    ForecastSnapshot,
    SnapshotSourceKind,
)
from pyracast.forecasts.sources import covering_ids
from pyracast.intervals import NormalizedInterval
from pyracast.irradiance import local_day_bounds
from pyracast.solar import daylight_window

ACTUAL_COMPOSITION_POLICY = "native-estimated-actuals-coverage-v1"
ACTUAL_PIN_FORMAT = "actual-composition-v1"


@dataclass(frozen=True)
class ArchivedActualSource:
    snapshot: ForecastSnapshot
    snapshots: list[ForecastSnapshot]
    composition: ForecastComposition | None
    cutoff: datetime

    @property
    def intervals(self) -> tuple[dict[str, Any], ...]:
        return (
            self.composition.intervals
            if self.composition is not None
            else tuple(self.snapshot.normalized_intervals)
        )

    @property
    def pin(self) -> int | dict[str, Any]:
        if self.composition is None:
            return self.snapshot.pk
        return {
            "format": ACTUAL_PIN_FORMAT,
            "snapshot_ids": list(self.composition.snapshot_ids),
            "segments": self.composition.segments,
            "intervals": list(self.composition.intervals),
            "prorated": self.composition.prorated,
        }


def single_snapshot_actual_queryset(
    binding: SiteBinding,
    start: datetime,
    end: datetime,
    *,
    collected_from: datetime,
    as_of: datetime,
) -> QuerySet[ForecastSnapshot]:
    return (
        ForecastSnapshot.objects.select_related("job__binding__provider_account")
        .defer("raw_payload")
        .filter(
            binding=binding,
            job__status=CollectionJobStatus.SUCCEEDED,
            source_kind=SnapshotSourceKind.ESTIMATED_ACTUALS,
            coverage_start__lt=end,
            coverage_end__gt=start,
            collected_at__gte=collected_from,
            collected_at__lte=as_of,
            available_at__lte=as_of,
        )
        .filter(Q(issued_at__isnull=True) | Q(issued_at__lte=as_of))
        .alias(
            source_priority=Case(
                When(
                    job__endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
                    job__reference_import__isnull=True,
                    then=Value(0),
                ),
                When(
                    job__endpoint=CollectionEndpoint.SCALED_ESTIMATED_ACTUALS,
                    capacity_normalization__source_snapshot__job__endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
                    then=Value(1),
                ),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("source_priority", "-collected_at", "-available_at", "-pk")
    )


def first_complete_actuals(
    candidates: Iterable[ForecastSnapshot], target_date: date, zone: str
) -> ForecastSnapshot | None:
    """The first candidate covering the whole day, else the first candidate."""
    latest = None
    for snapshot in candidates:
        if latest is None:
            latest = snapshot
        if history_day_complete(snapshot, target_date, zone):
            return snapshot
    return latest


def select_archived_actual_source(
    binding: SiteBinding, target_date: date, cutoff: datetime
) -> ArchivedActualSource | None:
    from pyracast.forecasts.history import select_actual_snapshot

    start, end = local_day_bounds(target_date, binding.timezone)
    return _archived_actual_source(
        binding,
        target_date,
        cutoff,
        select_actual_snapshot(binding, target_date, cutoff),
        native_actual_snapshots(binding, start, end, cutoff),
    )


@dataclass(frozen=True)
class ActualCandidates:
    """Every estimated-actual acquisition a range of days can select, read once; `select` makes
    the per-day choice of `select_archived_actual_source` from them."""

    binding: SiteBinding
    cutoff: datetime
    single_snapshots: tuple[ForecastSnapshot, ...]
    native: tuple[ForecastSnapshot, ...]

    def select(self, target_date: date) -> ArchivedActualSource | None:
        start, end = local_day_bounds(target_date, self.binding.timezone)
        single_snapshot = first_complete_actuals(
            (
                row
                for row in self.single_snapshots
                if _overlaps(row, start, end) and row.collected_at >= end
            ),
            target_date,
            self.binding.timezone,
        )
        overlapping = [row for row in self.native if _overlaps(row, start, end)]
        covering = set(
            covering_ids(
                ((row.pk, row.coverage_start, row.coverage_end) for row in overlapping),
                start,
                end,
            )
        )
        return _archived_actual_source(
            self.binding,
            target_date,
            self.cutoff,
            single_snapshot,
            [row for row in overlapping if row.pk in covering],
        )


def _overlaps(snapshot: ForecastSnapshot, start: datetime, end: datetime) -> bool:
    return (
        cast(datetime, snapshot.coverage_start) < end
        and cast(datetime, snapshot.coverage_end) > start
    )


def actual_candidates(
    binding: SiteBinding, first: date, last: date, cutoff: datetime
) -> ActualCandidates:
    start, first_end = local_day_bounds(first, binding.timezone)
    _, end = local_day_bounds(last, binding.timezone)
    return ActualCandidates(
        binding,
        cutoff,
        tuple(
            single_snapshot_actual_queryset(
                binding, start, end, collected_from=first_end, as_of=cutoff
            )
        ),
        tuple(native_actual_queryset(binding, start, end, cutoff)),
    )


def _archived_actual_source(
    binding: SiteBinding,
    target_date: date,
    cutoff: datetime,
    single_snapshot: ForecastSnapshot | None,
    snapshots: list[ForecastSnapshot],
) -> ArchivedActualSource | None:
    start, end = local_day_bounds(target_date, binding.timezone)
    if not snapshots:
        return (
            ArchivedActualSource(single_snapshot, [single_snapshot], None, cutoff)
            if single_snapshot is not None
            else None
        )
    source = _compose_native_source(binding, target_date, cutoff, snapshots)
    covered = (
        sum(
            (
                datetime.fromisoformat(row["end"]) - datetime.fromisoformat(row["start"])
            ).total_seconds()
            for row in source.intervals
        )
        == (end - start).total_seconds()
    )
    if (
        single_snapshot is not None
        and single_snapshot.job.endpoint != CollectionEndpoint.ESTIMATED_ACTUALS
        and history_day_complete(single_snapshot, target_date, binding.timezone)
        and not actual_coverage_complete(
            daylight_window(target_date, binding.timezone, binding.latitude, binding.longitude),
            start,
            end,
            source.intervals,
        )
    ):
        return ArchivedActualSource(single_snapshot, [single_snapshot], None, cutoff)
    if (
        len(source.snapshots) == 1
        and single_snapshot is not None
        and source.snapshot.pk == single_snapshot.pk
        and history_day_complete(single_snapshot, target_date, binding.timezone)
        and covered
    ):
        return ArchivedActualSource(single_snapshot, [single_snapshot], None, cutoff)
    return source


def _valid_actual_intervals(snapshot: ForecastSnapshot) -> tuple[dict[str, Any], ...]:
    intervals = []
    for row in snapshot.normalized_intervals:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(key), str) for key in ("start", "end")
        ):
            raise ValueError("Saved estimated-actual timestamps must be ISO strings.")
        start, end = (datetime.fromisoformat(row[key]) for key in ("start", "end"))
        if start.utcoffset() is None or end.utcoffset() is None or end <= start:
            continue
        intervals.append(row)
    return tuple(intervals)


def _compose_native_source(
    binding: SiteBinding,
    target_date: date,
    cutoff: datetime,
    snapshots: list[ForecastSnapshot],
) -> ArchivedActualSource:
    start, end = local_day_bounds(target_date, binding.timezone)
    selected = compose_forecasts(
        [
            ForecastVintage(
                snapshot.pk,
                cast(datetime, snapshot.coverage_start),
                cast(datetime, snapshot.coverage_end),
                _valid_actual_intervals(snapshot),
            )
            for snapshot in snapshots
        ],
        start,
        end,
    )
    intervals = tuple(
        {**row, "source_kind": "estimated_actuals"}
        for row in completed_actual_intervals(selected.intervals, cutoff)
    )
    owners = [row for row in snapshots if row.pk in selected.snapshot_ids]
    if not owners:
        raise ValueError("Pinned archived actual sources do not cover the selected day")
    composition = ForecastComposition(
        intervals, selected.segments, selected.snapshot_ids, selected.prorated
    )
    return ArchivedActualSource(owners[0], owners, composition, cutoff)


def replay_archived_actual_source(
    binding: SiteBinding, target_date: date, cutoff: datetime, pin: object
) -> ArchivedActualSource | None:
    if pin is None:
        return None
    if not isinstance(pin, dict):
        snapshot = (
            ForecastSnapshot.objects.select_related("job__reference_import", "binding")
            .filter(pk=cast(int, pin))
            .first()
        )
        return ArchivedActualSource(snapshot, [snapshot], None, cutoff) if snapshot else None
    if pin.get("format") != ACTUAL_PIN_FORMAT:
        raise ValueError("Unsupported archived actual selection")
    identifiers = pin["snapshot_ids"]
    if (
        not isinstance(identifiers, list)
        or not identifiers
        or any(type(identifier) is not int or identifier <= 0 for identifier in identifiers)
        or len(set(identifiers)) != len(identifiers)
    ):
        raise ValueError("Pinned archived actual source identities are invalid")
    indexed = {
        row.pk: row
        for row in ForecastSnapshot.objects.select_related("job__reference_import", "binding")
        .filter(
            pk__in=identifiers,
            binding=binding,
            source_kind=SnapshotSourceKind.ESTIMATED_ACTUALS,
            job__endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
            job__reference_import__isnull=True,
            job__status=CollectionJobStatus.SUCCEEDED,
            collected_at__lte=cutoff,
            available_at__lte=cutoff,
        )
        .filter(Q(issued_at__isnull=True) | Q(issued_at__lte=cutoff))
        .defer("raw_payload")
        .order_by("-collected_at", "-available_at", "-pk")
    }
    if set(indexed) != set(identifiers):
        raise ValueError("Pinned archived actual sources are unavailable")
    snapshots = list(indexed.values())
    source = _compose_native_source(binding, target_date, cutoff, snapshots)
    expected = json.dumps(source.pin, sort_keys=True, separators=(",", ":"), allow_nan=False)
    received = json.dumps(pin, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if expected != received:
        raise ValueError("Pinned archived actual selection does not match its immutable sources")
    return source


def apply_archived_actual_source(result: dict[str, Any], source: ArchivedActualSource) -> None:
    if source.composition is None:
        return
    apply_intraday_source(
        result,
        PreparedIntradaySource(
            source.snapshot,
            source.snapshots,
            source.composition,
            "estimated_actuals",
            source.cutoff,
            source.cutoff,
        ),
    )
    result["provenance"]["selection_policy"] = ACTUAL_COMPOSITION_POLICY


def archived_actual_irradiance(
    binding: SiteBinding, target_date: date, source: ArchivedActualSource
) -> dict[str, object]:
    result = calculate(
        binding,
        target_date,
        ProviderData(
            source.snapshot.raw_payload,
            NormalizedInterval.from_rows(source.intervals),
            source.snapshot.issued_at,
            source.snapshot.collected_at,
            source_kind="estimated_actuals",
        ),
    )
    result["reference_capacity_normalization"] = normalization_provenance(source.snapshot)
    if source.composition is not None:
        result["provenance"] = {
            "snapshot_id": source.snapshot.pk,
            "binding_id": binding.pk,
            "source_kind": "estimated_actuals",
        }
        apply_archived_actual_source(result, source)
        start, end = local_day_bounds(target_date, binding.timezone)
        cast(dict[str, object], result["provenance"])["input_day_complete"] = (
            sum(
                (
                    datetime.fromisoformat(row["end"]) - datetime.fromisoformat(row["start"])
                ).total_seconds()
                for row in source.intervals
            )
            == (end - start).total_seconds()
        )
    return result
