import hashlib
import json
import math
from datetime import UTC, date, datetime, timedelta

from pyracast.irradiance import local_day_bounds
from pyracast.weather import WeatherData

ATTRIBUTION = {
    "text": "Weather data by Open-Meteo.com",
    "url": "https://open-meteo.com/",
    "license": "CC BY 4.0",
    "license_url": "https://creativecommons.org/licenses/by/4.0/",
    "changes": "Intervals normalized; daily energy calculated by Pyracast.",
}


def unavailable_weather(reason: str) -> dict[str, object]:
    return {
        "status": "unavailable",
        "quality_flags": ["external_weather_unavailable", reason],
        "attribution": ATTRIBUTION.copy(),
        "source_kind": "external_weather_forecast",
        "native_interval_minutes": 60,
    }


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Weather timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _number(value: object, *, nonnegative: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("Invalid weather value")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError("Invalid weather value") from None
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise ValueError("Invalid weather value")
    return number


def summarize_weather(data: WeatherData, local_date: date, timezone: str) -> dict[str, object]:
    start, end = local_day_bounds(local_date, timezone)
    duration = (end - start).total_seconds()
    requested_start, requested_end = _utc(data.requested_start), _utc(data.requested_end)
    if requested_end <= requested_start:
        raise ValueError("Invalid requested window")
    collected = _utc(data.collected_at)
    if data.issued_at is not None:
        raise ValueError("Unexpected provider issue time")
    raw_json = json.dumps(data.raw_payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    rows: list[dict[str, object]] = []
    coverage = valid_coverage = energy = 0.0
    previous_end: datetime | None = None
    prorated = False
    for interval in data.intervals:
        native_start, native_end = _utc(interval.start), _utc(interval.end)
        if native_end - native_start != timedelta(hours=1):
            raise ValueError("Weather intervals must be one hour")
        if previous_end is not None and native_start < previous_end:
            raise ValueError("Weather intervals must be ordered and non-overlapping")
        previous_end = native_end
        ghi = _number(interval.ghi_wm2, nonnegative=True)
        temperature_start = _number(interval.temperature_start_c)
        temperature_end = _number(interval.temperature_end_c)
        snow_start = _number(interval.snow_depth_start_m, nonnegative=True)
        snow_end = _number(interval.snow_depth_end_m, nonnegative=True)
        snowfall = _number(interval.snowfall_cm, nonnegative=True)
        overlap_start, overlap_end = max(start, native_start), min(end, native_end)
        seconds = max(0.0, (overlap_end - overlap_start).total_seconds())
        if not seconds:
            continue
        coverage += seconds
        if ghi is not None:
            valid_coverage += seconds
            energy += ghi * (seconds / 3_600_000)
        partial_hour = seconds < 3600
        prorated |= partial_hour and ghi is not None
        rows.append(
            {
                "start": native_start.isoformat(),
                "end": native_end.isoformat(),
                "overlap_start": overlap_start.isoformat(),
                "overlap_end": overlap_end.isoformat(),
                "overlap_seconds": seconds,
                "full_interval": not partial_hour,
                "ghi_wm2": ghi,
                "temperature_start_c": temperature_start,
                "temperature_end_c": temperature_end,
                "snow_depth_start_m": snow_start,
                "snow_depth_end_m": snow_end,
                "snowfall_cm": snowfall,
            }
        )
    complete = valid_coverage == duration
    flags = ["independent_weather_model", "unvalidated_against_ground", "snow_loss_unknown"]
    if prorated:
        flags.append("uniform_mean_proration")
    return {
        "status": "ready" if complete else "partial",
        "source_kind": "external_weather_forecast",
        "provider": data.provider,
        "method_version": "open-meteo-hourly-ghi-v1",
        "local_date": local_date.isoformat(),
        "timezone": timezone,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "energy_kwh_m2": energy if complete else None,
        "partial_energy_kwh_m2": energy,
        "energy_mj_m2": energy * 3.6 if complete else None,
        "partial_energy_mj_m2": energy * 3.6,
        "coverage_fraction": valid_coverage / duration,
        "coverage_seconds": coverage,
        "valid_coverage_seconds": valid_coverage,
        "complete": complete,
        "native_interval_minutes": 60,
        "collected_at": collected.isoformat(),
        "issued_at": None,
        "requested_latitude": data.requested_latitude,
        "requested_longitude": data.requested_longitude,
        "grid_latitude": data.raw_payload.get("latitude"),
        "grid_longitude": data.raw_payload.get("longitude"),
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "raw_payload_sha256": hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
        "quality_flags": flags,
        "intervals": rows,
        "attribution": ATTRIBUTION.copy(),
    }
