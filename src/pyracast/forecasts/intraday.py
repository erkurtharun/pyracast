from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from typing import Any, Literal, cast

from django.db.models import Q

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.forecasts.sources import covering_snapshots, snapshot_data
from pyracast.irradiance import local_day_bounds
from pyracast.irradiance.weather_reference import summarize_weather, unavailable_weather
from pyracast.plants.forecast_composition import (
    ForecastComposition,
    ForecastVintage,
    compose_forecasts,
)
from pyracast.plants.forecast_sources import apply_composition, compose_snapshots
from pyracast.solar import Daylight, daylight_energy_windows, daylight_window

INTRADAY_POLICY = "estimated-actuals-then-forecast-v1"
type IntradaySourceKind = Literal["forecast", "estimated_actuals", "mixed"]


@dataclass(frozen=True)
class PreparedIntradaySource:
    snapshot: ForecastSnapshot
    snapshots: list[ForecastSnapshot]
    composition: ForecastComposition
    source_kind: IntradaySourceKind
    knowledge_cutoff: datetime
    elapsed_cutoff: datetime
    actuals_grace_until: datetime | None = None


def completed_actual_intervals(
    intervals: Sequence[dict[str, Any]], elapsed_cutoff: datetime
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for row in sorted(intervals, key=lambda item: datetime.fromisoformat(item["start"])):
        start, end = (datetime.fromisoformat(row[key]) for key in ("start", "end"))
        native_start = datetime.fromisoformat(row.get("original_start", row["start"]))
        native_end = datetime.fromisoformat(row.get("original_end", row["end"]))
        if (
            native_start.utcoffset() is None
            or native_end.utcoffset() is None
            or not native_start <= start < end <= native_end
        ):
            raise ValueError("Native estimated-actual bounds must contain their saved interval")
        power = row.get("power_kw")
        if (
            native_end <= elapsed_cutoff
            and not isinstance(power, bool)
            and isinstance(power, int | float)
            and isfinite(power)
            and power >= 0
        ):
            rows.append(row)
    return tuple(rows)


def _covers(
    rows: tuple[dict[str, Any], ...], windows: tuple[tuple[datetime, datetime], ...]
) -> bool:
    for left, right in windows:
        cursor = left
        for row in rows:
            start, end = (datetime.fromisoformat(row[key]) for key in ("start", "end"))
            if start <= cursor < end:
                cursor = end
            if cursor >= right:
                break
        if cursor < right:
            return False
    return bool(windows)


def _actual_source(
    binding: SiteBinding,
    start: datetime,
    end: datetime,
    daylight: Daylight,
    knowledge_cutoff: datetime,
    elapsed_cutoff: datetime,
    actuals_grace_until: datetime | None,
) -> tuple[list[ForecastSnapshot], tuple[dict[str, Any], ...], bool] | None:
    snapshots = native_actual_snapshots(
        binding, start, end, knowledge_cutoff, actuals_grace_until=actuals_grace_until
    )
    if not snapshots:
        return None
    composition = compose_snapshots(snapshots, start, end)
    rows = completed_actual_intervals(composition.intervals, elapsed_cutoff)
    used = {row["source_snapshot_id"] for row in rows}
    snapshots = [snapshot for snapshot in snapshots if snapshot.pk in used]
    return (snapshots, rows, actual_coverage_complete(daylight, start, end, rows)) if rows else None


def actual_coverage_complete(
    daylight: Daylight,
    start: datetime,
    end: datetime,
    rows: tuple[dict[str, Any], ...],
) -> bool:
    windows = daylight.windows or ((start, end),)
    native_windows = daylight_energy_windows(
        daylight,
        (
            (
                datetime.fromisoformat(row.get("original_start", row["start"])),
                datetime.fromisoformat(row.get("original_end", row["end"])),
            )
            for row in rows
        ),
    )
    required = (*windows, *((max(start, left), min(end, right)) for left, right in native_windows))
    return _covers(rows, required)


def native_actual_snapshots(
    binding: SiteBinding,
    start: datetime,
    end: datetime,
    knowledge_cutoff: datetime,
    *,
    actuals_grace_until: datetime | None = None,
) -> list[ForecastSnapshot]:
    eligible = _actuals_available_by(knowledge_cutoff)
    if actuals_grace_until is not None:
        eligible |= Q(job__scheduled_slot=knowledge_cutoff) & _actuals_available_by(
            actuals_grace_until
        )
    return covering_snapshots(
        ForecastSnapshot.objects.select_related("job", "binding")
        .filter(
            binding=binding,
            source_kind="estimated_actuals",
            job__endpoint="estimated_actuals",
            job__reference_import__isnull=True,
            job__status="succeeded",
            coverage_start__lt=end,
            coverage_end__gt=start,
        )
        .filter(eligible)
        .defer("raw_payload", "weather_payload")
        .order_by("-collected_at", "-available_at", "-pk"),
        start,
        end,
    )


def _actuals_available_by(cutoff: datetime) -> Q:
    return Q(collected_at__lte=cutoff, available_at__lte=cutoff) & (
        Q(issued_at__isnull=True) | Q(issued_at__lte=cutoff)
    )


def prepare_intraday_source(
    binding: SiteBinding,
    target_date: date,
    forecast_snapshots: list[ForecastSnapshot],
    *,
    knowledge_cutoff: datetime,
    elapsed_cutoff: datetime,
    actuals_only: bool = False,
    actuals_grace_until: datetime | None = None,
) -> PreparedIntradaySource | None:
    if any(
        moment.utcoffset() is None
        for moment in (knowledge_cutoff, elapsed_cutoff, actuals_grace_until)
        if moment is not None
    ):
        raise ValueError("Intraday cutoffs must be timezone-aware")
    start, end = local_day_bounds(target_date, binding.timezone)
    daylight = daylight_window(target_date, binding.timezone, binding.latitude, binding.longitude)
    actual = _actual_source(
        binding, start, end, daylight, knowledge_cutoff, elapsed_cutoff, actuals_grace_until
    )
    forecasts = [] if actuals_only or (actual is not None and actual[2]) else forecast_snapshots
    if any(row.binding_id != binding.pk or row.source_kind != "forecast" for row in forecasts):
        raise ValueError("Forecast inputs must belong to the selected binding and product")
    vintages: list[ForecastVintage] = []
    snapshots = list(forecasts)
    if actual is not None:
        actual_snapshots, rows, _ = actual
        snapshots = [*actual_snapshots, *snapshots]
        # Each valid completed native actual interval owns only its own duration.
        vintages.extend(
            ForecastVintage(
                row["source_snapshot_id"],
                datetime.fromisoformat(row["start"]),
                datetime.fromisoformat(row["end"]),
                (row,),
            )
            for row in rows
        )
    vintages.extend(
        ForecastVintage(
            snapshot.pk,
            cast(datetime, snapshot.coverage_start),
            cast(datetime, snapshot.coverage_end),
            tuple(snapshot.normalized_intervals),
        )
        for snapshot in forecasts
    )
    if not vintages:
        return None
    combined = compose_forecasts(vintages, start, end)
    identifiers = tuple(
        snapshot.pk for snapshot in snapshots if snapshot.pk in combined.snapshot_ids
    )
    kinds_by_id = {snapshot.pk: snapshot.source_kind for snapshot in snapshots}
    composition = ForecastComposition(
        tuple(
            {**row, "source_kind": kinds_by_id[row["source_snapshot_id"]]}
            for row in combined.intervals
        ),
        combined.segments,
        identifiers,
        combined.prorated,
    )
    snapshots = [snapshot for snapshot in snapshots if snapshot.pk in identifiers]
    kinds = {snapshot.source_kind for snapshot in snapshots}
    source_kind: IntradaySourceKind = (
        "mixed" if len(kinds) > 1 else cast(IntradaySourceKind, snapshots[0].source_kind)
    )
    return PreparedIntradaySource(
        snapshots[0],
        snapshots,
        composition,
        source_kind,
        knowledge_cutoff,
        elapsed_cutoff,
        actuals_grace_until,
    )


def apply_intraday_source(result: dict[str, Any], source: PreparedIntradaySource) -> None:
    apply_composition(result, source.snapshots, source.composition)
    if all(snapshot.source_kind == "forecast" for snapshot in source.snapshots):
        return
    provenance = result["provenance"]
    provenance.update(
        selection_policy=INTRADAY_POLICY,
        source_kind=source.source_kind,
        actuals_cutoff_at=source.knowledge_cutoff.isoformat(),
        elapsed_cutoff_at=source.elapsed_cutoff.isoformat(),
        collected_at=source.snapshot.collected_at.isoformat(),
    )
    if source.actuals_grace_until is not None:
        provenance["actuals_grace_until"] = source.actuals_grace_until.isoformat()
    indexed = {snapshot.pk: snapshot for snapshot in source.snapshots}
    provenance["source_segments"] = [dict(row) for row in provenance["source_segments"]]
    for row in provenance["source_snapshots"]:
        row["source_kind"] = indexed[row["snapshot_id"]].source_kind
    for row in provenance["source_segments"]:
        row["source_kind"] = indexed[row["snapshot_id"]].source_kind
    for row in provenance["source_intervals"]:
        row["source_kind"] = indexed[row["snapshot_id"]].source_kind
    for row in result["intervals"]:
        row["source_kinds"] = sorted(
            {indexed[identifier].source_kind for identifier in row["source_snapshot_ids"]}
        )
    if "vintage" in result:
        result["vintage"]["source_kind"] = source.source_kind
        result["vintage"]["scope"] = "primary_source"
    flags = result["quality_flags"]
    if "mixed_forecast_vintages" in flags:
        flags.remove("mixed_forecast_vintages")
    if source.source_kind == "mixed":
        flags.append("mixed_estimated_actuals_and_forecast")
    if sum(snapshot.source_kind == "estimated_actuals" for snapshot in source.snapshots) > 1:
        flags.append("mixed_estimated_actual_vintages")


def replay_intraday_composition(
    provenance: dict[str, Any], binding_id: int
) -> tuple[ForecastComposition, list[ForecastSnapshot]]:
    identifiers = provenance["source_snapshot_ids"]
    indexed = {
        snapshot.pk: snapshot
        for snapshot in ForecastSnapshot.objects.filter(
            pk__in=identifiers,
            binding_id=binding_id,
            source_kind__in=("forecast", "estimated_actuals"),
        ).defer("raw_payload", "weather_payload")
    }
    if set(indexed) != set(identifiers):
        raise ValueError("Pinned intraday sources are unavailable")
    intervals = []
    for selected in provenance["source_intervals"]:
        snapshot = indexed[selected["snapshot_id"]]
        original_start = datetime.fromisoformat(selected["original_start"])
        original_end = datetime.fromisoformat(selected["original_end"])
        native = next(
            (
                row
                for row in snapshot.normalized_intervals
                if datetime.fromisoformat(row.get("original_start", row["start"])) == original_start
                and datetime.fromisoformat(row.get("original_end", row["end"])) == original_end
            ),
            None,
        )
        if native is None:
            raise ValueError("Pinned native intraday interval is unavailable")
        row = {**native, "source_snapshot_id": snapshot.pk, "source_kind": snapshot.source_kind}
        if selected["start"] != native["start"] or selected["end"] != native["end"]:
            row.update(
                start=selected["start"],
                end=selected["end"],
                original_start=selected["original_start"],
                original_end=selected["original_end"],
            )
        intervals.append(row)
    return (
        ForecastComposition(
            tuple(intervals),
            provenance["source_segments"],
            tuple(identifiers),
            any("original_start" in row for row in intervals),
        ),
        [indexed[identifier] for identifier in identifiers],
    )


def preserve_forecast_weather(
    result: dict[str, Any],
    binding: SiteBinding,
    target_date: date,
    forecasts: list[ForecastSnapshot],
) -> None:
    if not forecasts:
        return
    data = snapshot_data(forecasts[0])
    if data.weather is not None:
        result["weather_reference"] = summarize_weather(data.weather, target_date, binding.timezone)
    elif data.weather_error:
        result["weather_reference"] = unavailable_weather(data.weather_error)
