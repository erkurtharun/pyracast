from typing import cast

from pyracast.connections.contracts import ProviderData
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import SourceKind
from pyracast.weather import WeatherError, restore_weather


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
