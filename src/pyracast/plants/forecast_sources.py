from datetime import datetime
from typing import Any, cast

from pyracast.forecasts.composition import (
    ForecastComposition,
    ForecastVintage,
    compose_forecasts,
)
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.forecasts.sources import snapshot_rows
from pyracast.plants.models import PlantConfiguration

SELECTION_POLICY = "latest-forecast-coverage-v2"


def compose_snapshots(
    snapshots: list[ForecastSnapshot], start: datetime, end: datetime
) -> ForecastComposition:
    return compose_forecasts(
        [
            ForecastVintage(
                snapshot.pk,
                cast(datetime, snapshot.coverage_start),
                cast(datetime, snapshot.coverage_end),
                snapshot_rows(snapshot),
            )
            for snapshot in snapshots
        ],
        start,
        end,
    )


def replay_composition(
    provenance: dict[str, Any], configuration: PlantConfiguration, start: datetime, end: datetime
) -> tuple[ForecastComposition, list[ForecastSnapshot]] | None:
    identifiers = provenance.get("source_snapshot_ids")
    if identifiers is None:
        return None
    if provenance.get("selection_policy") in (
        "estimated-actuals-then-forecast-v1",
        "native-estimated-actuals-coverage-v1",
    ):
        from pyracast.forecasts.intraday import replay_intraday_composition

        return replay_intraday_composition(provenance, configuration.binding_id)
    snapshots = ForecastSnapshot.objects.filter(
        pk__in=identifiers, job__binding_id=configuration.binding_id, source_kind="forecast"
    ).defer("raw_payload")
    indexed = {snapshot.pk: snapshot for snapshot in snapshots}
    ordered = [indexed[identifier] for identifier in identifiers]
    return compose_snapshots(ordered, start, end), ordered


def apply_composition(
    result: dict[str, Any], snapshots: list[ForecastSnapshot], composition: ForecastComposition
) -> None:
    indexed = {snapshot.pk: snapshot for snapshot in snapshots}
    used = [indexed[identifier] for identifier in composition.snapshot_ids]
    result["provenance"].update(
        selection_policy=SELECTION_POLICY,
        source_snapshot_ids=list(composition.snapshot_ids),
        source_snapshots=[
            {
                "snapshot_id": snapshot.pk,
                "collected_at": snapshot.collected_at.isoformat(),
                "available_at": cast(datetime, snapshot.available_at).isoformat(),
                "issued_at": snapshot.issued_at.isoformat() if snapshot.issued_at else None,
                "coverage_start": cast(datetime, snapshot.coverage_start).isoformat(),
                "coverage_end": cast(datetime, snapshot.coverage_end).isoformat(),
            }
            for snapshot in used
        ],
        source_segments=composition.segments,
        source_intervals=[
            {
                "snapshot_id": row["source_snapshot_id"],
                "start": row["start"],
                "end": row["end"],
                "original_start": row.get("original_start", row["start"]),
                "original_end": row.get("original_end", row["end"]),
            }
            for row in composition.intervals
        ],
        oldest_collected_at=min(snapshot.collected_at for snapshot in used).isoformat(),
    )
    owners = [
        (
            datetime.fromisoformat(cast(str, segment["start"])),
            datetime.fromisoformat(cast(str, segment["end"])),
            cast(int, segment["snapshot_id"]),
        )
        for segment in composition.segments
    ]
    for interval in result["intervals"]:
        start, end = (datetime.fromisoformat(interval[key]) for key in ("start", "end"))
        interval["source_snapshot_ids"] = sorted(
            {identifier for left, right, identifier in owners if left < end and right > start}
        )
    if len(composition.snapshot_ids) > 1:
        result["quality_flags"].append("mixed_forecast_vintages")
    if composition.prorated:
        result["quality_flags"].append("uniform_mean_proration")
