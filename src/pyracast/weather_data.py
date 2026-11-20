import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal
from urllib.parse import unquote, unquote_plus

HOURLY_UNITS = {
    "time": "unixtime",
    "temperature_2m": "°C",
    "snow_depth": "m",
    "snowfall": "cm",
    "shortwave_radiation": "W/m²",
}


@dataclass(frozen=True)
class WeatherInterval:
    start: datetime
    end: datetime
    ghi_wm2: float | None
    temperature_start_c: float | None
    temperature_end_c: float | None
    snow_depth_start_m: float | None
    snow_depth_end_m: float | None
    snowfall_cm: float | None


@dataclass(frozen=True)
class WeatherData:
    raw_payload: dict[str, object]
    intervals: tuple[WeatherInterval, ...]
    collected_at: datetime
    requested_latitude: float
    requested_longitude: float
    requested_start: datetime
    requested_end: datetime
    provider: Literal["open_meteo"] = "open_meteo"
    source_kind: Literal["weather_forecast"] = "weather_forecast"
    issued_at: datetime | None = None


class WeatherFailure(StrEnum):
    INVALID_NUMBER = "invalid_weather_number"
    INVALID_COORDINATES = "invalid_weather_coordinates"
    INVALID_SCHEMA = "invalid_weather_schema"
    INVALID_TIMEZONE = "invalid_weather_timezone"
    INVALID_UNITS = "invalid_weather_units"
    EMPTY_RESPONSE = "empty_weather_response"
    INVALID_INTERVAL = "invalid_weather_interval"
    INVALID_WINDOW = "invalid_weather_window"
    UNSAFE_RESPONSE = "unsafe_weather_response"
    INVALID_COLLECTION_TIME = "invalid_weather_collection_time"
    REQUEST_FAILED = "weather_request_failed"


class WeatherError(Exception):
    def __init__(self, code: WeatherFailure) -> None:
        self.code = code
        super().__init__(code)


def _weather_number(value: object, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise WeatherError(WeatherFailure.INVALID_NUMBER)
    try:
        number = float(value)
    except OverflowError:
        raise WeatherError(WeatherFailure.INVALID_NUMBER) from None
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise WeatherError(WeatherFailure.INVALID_NUMBER)
    return number


def weather_coordinate(value: object, limit: int) -> float:
    number = _weather_number(value)
    if abs(number) > limit:
        raise WeatherError(WeatherFailure.INVALID_COORDINATES)
    return number


def unsafe_weather_payload(value: object, secret: str | None) -> bool:
    if isinstance(value, str):
        return bool(
            secret and any(secret in form for form in (value, unquote(value), unquote_plus(value)))
        )
    if isinstance(value, list):
        return any(unsafe_weather_payload(item, secret) for item in value)
    if isinstance(value, dict):
        return any(
            str(key).lower().replace("_", "").replace("-", "")
            in {"apikey", "authorization", "accesstoken", "password", "credentials"}
            or unsafe_weather_payload(key, secret)
            or unsafe_weather_payload(item, secret)
            for key, item in value.items()
        )
    return False


def _weather_columns(payload: object) -> tuple[dict[str, object], dict[str, list[object]]]:
    if not isinstance(payload, dict):
        raise WeatherError(WeatherFailure.INVALID_SCHEMA)
    try:
        json.dumps(payload, allow_nan=False)
    except TypeError, ValueError:
        raise WeatherError(WeatherFailure.INVALID_SCHEMA) from None
    weather_coordinate(payload.get("latitude"), 90)
    weather_coordinate(payload.get("longitude"), 180)
    offset = payload.get("utc_offset_seconds")
    if isinstance(offset, bool) or offset != 0:
        raise WeatherError(WeatherFailure.INVALID_TIMEZONE)
    units = payload.get("hourly_units")
    if not isinstance(units, dict) or any(
        units.get(name) != unit for name, unit in HOURLY_UNITS.items()
    ):
        raise WeatherError(WeatherFailure.INVALID_UNITS)
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise WeatherError(WeatherFailure.INVALID_SCHEMA)
    columns: dict[str, list[object]] = {}
    for name in HOURLY_UNITS:
        values = hourly.get(name)
        if not isinstance(values, list):
            raise WeatherError(WeatherFailure.INVALID_SCHEMA)
        columns[name] = values
    count = len(columns["time"])
    if count == 0:
        raise WeatherError(WeatherFailure.EMPTY_RESPONSE)
    if any(len(values) != count for values in columns.values()):
        raise WeatherError(WeatherFailure.INVALID_SCHEMA)
    return payload.copy(), columns


def _weather_intervals(columns: dict[str, list[object]]) -> tuple[WeatherInterval, ...]:
    intervals: list[WeatherInterval] = []
    previous_end: datetime | None = None
    previous_temperature: float | None = None
    previous_snow_depth: float | None = None
    for index, timestamp in enumerate(columns["time"]):
        seconds = _weather_number(timestamp)
        if not seconds.is_integer() or seconds % 3600:
            raise WeatherError(WeatherFailure.INVALID_INTERVAL)
        try:
            end = datetime.fromtimestamp(seconds, UTC)
            start = end - timedelta(hours=1)
        except OverflowError, OSError, ValueError:
            raise WeatherError(WeatherFailure.INVALID_INTERVAL) from None
        if previous_end is not None and previous_end != start:
            raise WeatherError(WeatherFailure.INVALID_INTERVAL)
        values = {
            name: None
            if (value := column[index]) is None
            else _weather_number(value, nonnegative=name != "temperature_2m")
            for name, column in columns.items()
            if name != "time"
        }
        # Radiation and snowfall describe the preceding hour; the other values are instants.
        intervals.append(
            WeatherInterval(
                start=start,
                end=end,
                ghi_wm2=values["shortwave_radiation"],
                temperature_start_c=previous_temperature,
                temperature_end_c=values["temperature_2m"],
                snow_depth_start_m=previous_snow_depth,
                snow_depth_end_m=values["snow_depth"],
                snowfall_cm=values["snowfall"],
            )
        )
        previous_end = end
        previous_temperature = values["temperature_2m"]
        previous_snow_depth = values["snow_depth"]
    return tuple(intervals)


def weather_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.utcoffset() is None or end.utcoffset() is None:
        raise WeatherError(WeatherFailure.INVALID_WINDOW)
    try:
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        start - timedelta(hours=1)
    except OverflowError:
        raise WeatherError(WeatherFailure.INVALID_WINDOW) from None
    if end <= start or end - start > timedelta(days=16):
        raise WeatherError(WeatherFailure.INVALID_WINDOW)
    return start, end


def normalize_weather(
    payload: object,
    *,
    latitude: float,
    longitude: float,
    start: datetime,
    end: datetime,
    collected_at: datetime | None = None,
) -> WeatherData:
    latitude = weather_coordinate(latitude, 90)
    longitude = weather_coordinate(longitude, 180)
    start, end = weather_window(start, end)
    if unsafe_weather_payload(payload, None):
        raise WeatherError(WeatherFailure.UNSAFE_RESPONSE)
    raw, columns = _weather_columns(payload)
    intervals = tuple(
        interval
        for interval in _weather_intervals(columns)
        if interval.start < end and interval.end > start
    )
    if not intervals:
        raise WeatherError(WeatherFailure.EMPTY_RESPONSE)
    collected = collected_at if collected_at is not None else datetime.now(UTC)
    if collected.utcoffset() is None:
        raise WeatherError(WeatherFailure.INVALID_COLLECTION_TIME)
    return WeatherData(
        raw_payload=raw,
        intervals=intervals,
        collected_at=collected.astimezone(UTC),
        requested_latitude=latitude,
        requested_longitude=longitude,
        requested_start=start,
        requested_end=end,
    )


def serialize_weather(data: WeatherData) -> dict[str, object]:
    return {
        "provider": data.provider,
        "source_kind": data.source_kind,
        "collected_at": data.collected_at.isoformat(),
        "issued_at": None,
        "requested_latitude": data.requested_latitude,
        "requested_longitude": data.requested_longitude,
        "requested_start": data.requested_start.isoformat(),
        "requested_end": data.requested_end.isoformat(),
        "raw_payload": data.raw_payload,
    }


def _saved_weather_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise WeatherError(WeatherFailure.INVALID_SCHEMA)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise WeatherError(WeatherFailure.INVALID_SCHEMA) from None


def restore_weather(payload: object) -> WeatherData:
    try:
        if (
            not isinstance(payload, dict)
            or payload.get("provider") != "open_meteo"
            or payload.get("source_kind") != "weather_forecast"
            or payload.get("issued_at") is not None
            or unsafe_weather_payload(payload, None)
        ):
            raise WeatherError(WeatherFailure.INVALID_SCHEMA)
        return normalize_weather(
            payload.get("raw_payload"),
            latitude=weather_coordinate(payload.get("requested_latitude"), 90),
            longitude=weather_coordinate(payload.get("requested_longitude"), 180),
            start=_saved_weather_datetime(payload.get("requested_start")),
            end=_saved_weather_datetime(payload.get("requested_end")),
            collected_at=_saved_weather_datetime(payload.get("collected_at")),
        )
    except RecursionError:
        raise WeatherError(WeatherFailure.INVALID_SCHEMA) from None
