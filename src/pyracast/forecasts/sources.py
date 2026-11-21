from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Literal, cast

from django.db.models import Q, QuerySet

from pyracast.connections.contracts import ProviderData
from pyracast.errors import BusyError, ErrorCode
from pyracast.forecasts.models import CollectionJobStatus, ForecastSnapshot, SnapshotWeatherError
from pyracast.intervals import NormalizedInterval
from pyracast.irradiance import SourceKind
from pyracast.weather_data import WeatherError, restore_weather


def mark_forecast_fallback(payload: dict[str, object], cutoff: datetime) -> None:
    flags = payload.get("quality_flags")
    payload["quality_flags"] = [*(flags if isinstance(flags, list) else []), "forecast_fallback"]
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
        payload["provenance"] = provenance
    provenance.update(
        fallback_reason="estimated_actuals_unavailable", selection_cutoff=cutoff.isoformat()
    )


def current_forecast_snapshots(
    *, binding_id: int, start: datetime, end: datetime, as_of: datetime
) -> list[ForecastSnapshot]:
    return covering_snapshots(
        ForecastSnapshot.objects.select_related("job")
        .filter(
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
        .order_by("-collected_at", "-available_at", "-pk"),
        start,
        end,
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
    weather = None
    weather_error: SnapshotWeatherError | Literal[""] = (
        SnapshotWeatherError(snapshot.weather_error) if snapshot.weather_error else ""
    )
    if snapshot.weather_payload is not None:
        try:
            weather = restore_weather(snapshot.weather_payload)
        except WeatherError as exc:
            weather_error = SnapshotWeatherError(exc.code)
    return ProviderData(
        raw_payload=snapshot.raw_payload,
        intervals=NormalizedInterval.from_rows(
            snapshot.normalized_intervals if intervals is None else intervals
        ),
        issued_at=snapshot.issued_at,
        collected_at=snapshot.collected_at,
        source_kind=cast(SourceKind, snapshot.source_kind),
        weather=weather,
        weather_error=weather_error,
    )
