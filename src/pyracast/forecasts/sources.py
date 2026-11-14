from datetime import datetime
from typing import cast

from django.db.models import Q

from pyracast.connections.contracts import ProviderData
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import SourceKind
from pyracast.weather import WeatherError, restore_weather


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
    return list(
        ForecastSnapshot.objects.select_related("job")
        .filter(
            binding_id=binding_id,
            job__status="succeeded",
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


def snapshot_data(
    snapshot: ForecastSnapshot, *, intervals: list[dict[str, object]] | None = None
) -> ProviderData:
    weather = None
    weather_error = snapshot.weather_error
    if snapshot.weather_payload is not None:
        try:
            weather = restore_weather(snapshot.weather_payload)
        except WeatherError as exc:
            weather_error = exc.code
    return ProviderData(
        raw_payload=snapshot.raw_payload,
        intervals=snapshot.normalized_intervals if intervals is None else intervals,
        issued_at=snapshot.issued_at,
        collected_at=snapshot.collected_at,
        source_kind=cast(SourceKind, snapshot.source_kind),
        weather=weather,
        weather_error=weather_error,
    )
