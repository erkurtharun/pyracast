import json
import logging
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import httpx
import pytest

from pyracast.weather import (
    OpenMeteoProvider,
    WeatherData,
    WeatherFailure,
    normalize_weather,
    restore_weather,
    serialize_weather,
)

START = datetime(2026, 11, 11, tzinfo=UTC)
END = START + timedelta(hours=2)


def weather_payload() -> dict[str, object]:
    # Documented Open-Meteo hourly response shape; this is not a live acquisition.
    return {
        "latitude": 39.0,
        "longitude": 32.875,
        "generationtime_ms": 0.18,
        "utc_offset_seconds": 0,
        "timezone": "GMT",
        "hourly_units": {
            "time": "unixtime",
            "temperature_2m": "°C",
            "snow_depth": "m",
            "snowfall": "cm",
            "shortwave_radiation": "W/m²",
        },
        "hourly": {
            "time": [1794351600, 1794355200, 1794358800, 1794362400, 1794366000],
            "temperature_2m": [-4.0, -2.0, 0.0, 2.0, 4.0],
            "snow_depth": [0.1, 0.12, 0.15, 0.1, 0.0],
            "snowfall": [0.0, 2.0, 3.0, 0.0, 0.0],
            "shortwave_radiation": [0.0, 0.0, 120.0, 240.0, 350.0],
        },
    }


def normalize(payload: object, start: datetime = START, end: datetime = END) -> WeatherData:
    return normalize_weather(
        payload, latitude=39.1, longitude=32.9, start=start, end=end, collected_at=START
    )


def test_documented_hourly_fixture_preserves_units_bounds_endpoints_and_provenance() -> None:
    raw = weather_payload()
    result = normalize(raw)
    assert result.raw_payload == raw
    assert result.raw_payload is not raw
    assert result.provider == "open_meteo"
    assert result.source_kind == "weather_forecast"
    assert result.issued_at is None
    assert result.collected_at == START
    assert result.requested_latitude == 39.1
    assert result.requested_longitude == 32.9
    assert result.requested_start == START
    assert result.requested_end == END
    assert len(result.intervals) == 2
    first, second = result.intervals
    assert (first.start, first.end) == (START, START + timedelta(hours=1))
    assert first.ghi_wm2 == 120
    assert first.temperature_start_c == -2
    assert first.temperature_end_c == 0
    assert first.snow_depth_start_m == 0.12
    assert first.snow_depth_end_m == 0.15
    assert first.snowfall_cm == 3
    assert second.ghi_wm2 == 240
    assert second.snowfall_cm == 0
    assert sum(cast(float, row.ghi_wm2) for row in result.intervals) == 360
    with pytest.raises(FrozenInstanceError):
        first.__setattr__("ghi_wm2", 8)


def test_filter_keeps_native_hours_and_missing_endpoints_without_fabrication() -> None:
    raw = weather_payload()
    hourly = cast(dict[str, list[object]], raw["hourly"])
    for name in ("temperature_2m", "snow_depth", "snowfall", "shortwave_radiation"):
        hourly[name][2] = None
    start = START + timedelta(minutes=15)
    result = normalize(raw, start, START + timedelta(minutes=45))
    assert len(result.intervals) == 1
    interval = result.intervals[0]
    assert interval.start == START
    assert interval.end == START + timedelta(hours=1)
    assert interval.ghi_wm2 is None
    assert interval.temperature_start_c == -2
    assert interval.temperature_end_c is None
    assert interval.snow_depth_end_m is None
    assert interval.snowfall_cm is None
    earliest = normalize(raw, START - timedelta(hours=2), START - timedelta(hours=1)).intervals[0]
    assert earliest.temperature_start_c is None
    assert earliest.snow_depth_start_m is None
    next_interval = normalize(raw).intervals[1]
    assert next_interval.temperature_start_c is None
    assert next_interval.snow_depth_start_m is None


def test_request_is_utc_bounded_fixed_host_and_without_authentication() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == "api.open-meteo.com"
        assert request.url.path == "/v1/forecast"
        assert "apikey" not in request.url.params
        assert "authorization" not in request.headers
        assert request.url.params["start_date"] == "2026-11-10"
        assert request.url.params["end_date"] == "2026-11-11"
        assert request.url.params["timezone"] == "UTC"
        assert request.url.params["timeformat"] == "unixtime"
        assert request.url.params["temperature_unit"] == "celsius"
        assert request.url.params["precipitation_unit"] == "mm"
        assert set(request.url.params["hourly"].split(",")) == {
            "temperature_2m",
            "snow_depth",
            "snowfall",
            "shortwave_radiation",
        }
        assert request.extensions["timeout"] == {"connect": 5, "read": 20, "write": 20, "pool": 20}
        return httpx.Response(200, json=weather_payload())

    result = OpenMeteoProvider(httpx.MockTransport(respond)).fetch(39.1, 32.9, START, END)
    assert result.intervals == normalize(weather_payload()).intervals
    assert result.collected_at.utcoffset() == timedelta(0)
    assert len(requests) == 1


def test_window_ending_at_2330_requests_next_midnight_without_splitting_hour() -> None:
    start = datetime(2026, 11, 11, 23, tzinfo=UTC)
    end = datetime(2026, 11, 11, 23, 30, tzinfo=UTC)
    raw = weather_payload()
    raw["hourly"] = {
        "time": [1794438000, 1794441600],
        "temperature_2m": [1.0, 2.0],
        "snow_depth": [0.0, 0.0],
        "snowfall": [0.0, 0.0],
        "shortwave_radiation": [0.0, 50.0],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["end_date"] == "2026-11-12"
        return httpx.Response(200, json=raw)

    result = OpenMeteoProvider(httpx.MockTransport(respond)).fetch(39, 32, start, end)
    assert len(result.intervals) == 1
    interval = result.intervals[0]
    assert interval.start == start
    assert interval.end == datetime(2026, 11, 12, tzinfo=UTC)
    assert interval.ghi_wm2 == 50
    assert interval.temperature_start_c == 1
    assert interval.temperature_end_c == 2
    assert result.requested_end == end


def test_customer_key_uses_fixed_customer_host_without_saved_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="httpx")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "customer-api.open-meteo.com"
        assert request.url.params["apikey"] == "private-key"
        return httpx.Response(200, json=weather_payload())

    result = OpenMeteoProvider(httpx.MockTransport(respond), "private-key").fetch(
        39.1, 32.9, START, END
    )
    assert "private-key" not in json.dumps(serialize_weather(result))
    assert "HTTP Request:" in caplog.text
    assert "private-key" not in caplog.text
    assert "apikey" not in caplog.text


def test_customer_default_transport_keeps_key_below_client_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.params["apikey"] == "private-key"
        return httpx.Response(200, json=weather_payload())

    monkeypatch.setattr(httpx, "HTTPTransport", lambda: httpx.MockTransport(respond))
    result = OpenMeteoProvider(api_key="private-key").fetch(39, 32, START, END)
    assert len(calls) == 1
    assert result.issued_at is None


@pytest.mark.parametrize("status", [301, 400, 429, 500])
def test_http_failures_do_not_follow_redirects_retry_or_expose_bodies(status: int) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, text="private-key", headers={"location": "https://other.test"}
        )

    with pytest.raises(WeatherFailure, match=r"^weather_request_failed$") as failure:
        OpenMeteoProvider(httpx.MockTransport(respond), "private-key").fetch(39, 32, START, END)
    assert failure.value.code == "weather_request_failed"
    assert "private-key" not in str(failure.value)
    assert calls == 1


@pytest.mark.parametrize(
    "response", [b"not-json", b'"\xff"', b"9" * 5000, b"[" * 2000 + b"]" * 2000]
)
def test_invalid_response_decoding_is_sanitized(response: bytes) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=response))
    with pytest.raises(WeatherFailure, match="weather_request_failed"):
        OpenMeteoProvider(transport).fetch(39, 32, START, END)


def test_transport_failures_are_sanitized() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private-key", request=request)

    with pytest.raises(WeatherFailure, match=r"^weather_request_failed$"):
        OpenMeteoProvider(httpx.MockTransport(fail)).fetch(39, 32, START, END)


@pytest.mark.parametrize("echo", ["a%2Fb%2Bc", "a%2fb%2bc"])
def test_percent_encoded_echoed_keys_are_never_saved(echo: str) -> None:
    raw = weather_payload()
    raw["metadata"] = f"https://example.test/?apikey={echo}"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=raw))
    with pytest.raises(WeatherFailure, match="unsafe_weather_response"):
        OpenMeteoProvider(transport, "a/b+c").fetch(39, 32, START, END)


@pytest.mark.parametrize(
    "metadata", ["private-key", ["private-key"], {"private-key": 1}, {"nested": "private-key"}]
)
def test_echoed_secrets_are_rejected_recursively(metadata: object) -> None:
    raw = weather_payload()
    raw["metadata"] = metadata
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=raw))
    with pytest.raises(WeatherFailure, match=r"^unsafe_weather_response$"):
        OpenMeteoProvider(transport, "private-key").fetch(39, 32, START, END)


@pytest.mark.parametrize("key", ["apikey", "API_KEY", "Authorization", "access-token", "password"])
def test_authentication_fields_are_never_saved(key: str) -> None:
    raw = weather_payload()
    raw["metadata"] = {key: "another-secret"}
    with pytest.raises(WeatherFailure, match="unsafe_weather_response"):
        normalize(raw)


@pytest.mark.parametrize("payload", [None, [], {}, {"latitude": 1, "longitude": 2}])
def test_invalid_response_schemas_are_rejected(payload: object) -> None:
    with pytest.raises(WeatherFailure):
        normalize(payload)


@pytest.mark.parametrize("metadata", [object(), float("nan"), float("inf")])
def test_non_json_or_nonfinite_metadata_is_rejected(metadata: object) -> None:
    raw = weather_payload()
    raw["metadata"] = metadata
    with pytest.raises(WeatherFailure, match="schema"):
        normalize(raw)


@pytest.mark.parametrize("offset", [True, None, "0", 3600])
def test_non_utc_timezone_is_rejected(offset: object) -> None:
    raw = weather_payload()
    raw["utc_offset_seconds"] = offset
    with pytest.raises(WeatherFailure, match="timezone"):
        normalize(raw)


@pytest.mark.parametrize("units", [None, {}, {"time": "iso8601"}])
def test_unit_schema_is_required(units: object) -> None:
    raw = weather_payload()
    raw["hourly_units"] = units
    with pytest.raises(WeatherFailure, match="units"):
        normalize(raw)


@pytest.mark.parametrize(
    "name", ["time", "temperature_2m", "snow_depth", "snowfall", "shortwave_radiation"]
)
def test_each_unit_must_match_exactly(name: str) -> None:
    raw = weather_payload()
    cast(dict[str, object], raw["hourly_units"])[name] = "wrong"
    with pytest.raises(WeatherFailure, match="units"):
        normalize(raw)


@pytest.mark.parametrize("hourly", [None, [], {}, {"time": [1]}])
def test_hourly_schema_is_required(hourly: object) -> None:
    raw = weather_payload()
    raw["hourly"] = hourly
    with pytest.raises(WeatherFailure, match="schema"):
        normalize(raw)


@pytest.mark.parametrize("value", [None, "bad", [], [1]])
def test_inconsistent_or_missing_hourly_arrays_are_rejected(value: object) -> None:
    raw = weather_payload()
    cast(dict[str, object], raw["hourly"])["temperature_2m"] = value
    with pytest.raises(WeatherFailure, match="schema"):
        normalize(raw)


@pytest.mark.parametrize("value", [True, "0", float("nan"), float("inf"), 10**1000])
def test_invalid_numbers_are_rejected(value: object) -> None:
    raw = weather_payload()
    cast(dict[str, list[object]], raw["hourly"])["temperature_2m"][0] = value
    with pytest.raises(WeatherFailure):
        normalize(raw)


@pytest.mark.parametrize("name", ["snow_depth", "snowfall", "shortwave_radiation"])
def test_negative_nonnegative_weather_variables_are_rejected(name: str) -> None:
    raw = weather_payload()
    cast(dict[str, list[object]], raw["hourly"])[name][0] = -0.1
    with pytest.raises(WeatherFailure, match="number"):
        normalize(raw)


@pytest.mark.parametrize(
    "time", [0.5, 1, True, 1794351600, 1794358800, 3600 * 10**15, -62135596800]
)
def test_invalid_duplicate_gapped_or_out_of_range_timestamps_are_rejected(time: object) -> None:
    raw = weather_payload()
    cast(dict[str, list[object]], raw["hourly"])["time"][1] = time
    with pytest.raises(WeatherFailure):
        normalize(raw)


def test_empty_rows_and_nonoverlapping_forecasts_are_not_zero_weather() -> None:
    raw = weather_payload()
    raw["hourly"] = {name: [] for name in cast(dict[str, object], raw["hourly"])}
    with pytest.raises(WeatherFailure, match="empty_weather_response"):
        normalize(raw)
    with pytest.raises(WeatherFailure, match="empty_weather_response"):
        normalize(weather_payload(), END + timedelta(days=1), END + timedelta(days=2))


@pytest.mark.parametrize(
    ("latitude", "longitude"), [(91, 0), (0, -181), (True, 0), (0, float("nan"))]
)
def test_invalid_coordinates_fail_before_http(latitude: float, longitude: float) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid coordinates must not request weather")

    with pytest.raises(WeatherFailure):
        OpenMeteoProvider(httpx.MockTransport(fail)).fetch(latitude, longitude, START, END)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (START.replace(tzinfo=None), END),
        (START, END.replace(tzinfo=None)),
        (END, START),
        (START, START),
        (START, START + timedelta(days=17)),
        (datetime.min.replace(tzinfo=UTC), START),
    ],
)
def test_invalid_windows_fail_before_http(start: datetime, end: datetime) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid windows must not request weather")

    with pytest.raises(WeatherFailure, match="window"):
        OpenMeteoProvider(httpx.MockTransport(fail)).fetch(39, 32, start, end)


def test_collected_time_requires_an_offset() -> None:
    with pytest.raises(WeatherFailure, match="collection_time"):
        normalize_weather(
            weather_payload(),
            latitude=39,
            longitude=32,
            start=START,
            end=END,
            collected_at=START.replace(tzinfo=None),
        )


def test_requested_and_collected_offsets_are_normalized_to_utc() -> None:
    local = timezone(timedelta(hours=3))
    result = normalize_weather(
        weather_payload(),
        latitude=-90,
        longitude=180,
        start=START.astimezone(local),
        end=END.astimezone(local),
        collected_at=START.astimezone(local),
    )
    assert result.requested_start == START
    assert result.requested_start.tzinfo is UTC
    assert result.requested_end.tzinfo is UTC
    assert result.collected_at.tzinfo is UTC


def test_saved_weather_roundtrip_revalidates_raw_without_invented_issue_time() -> None:
    result = normalize(weather_payload())
    saved = serialize_weather(result)
    assert saved["requested_start"] == START.isoformat()
    assert saved["issued_at"] is None
    assert restore_weather(json.loads(json.dumps(saved))) == result
    raw = cast(dict[str, object], saved["raw_payload"])
    raw["hourly_units"] = {}
    with pytest.raises(WeatherFailure, match="units"):
        restore_weather(saved)


def test_saved_weather_rejects_excessive_json_nesting() -> None:
    saved = serialize_weather(normalize(weather_payload()))
    nested: object = None
    for _ in range(2000):
        nested = [nested]
    saved["metadata"] = nested
    with pytest.raises(WeatherFailure, match="invalid_weather_schema"):
        restore_weather(saved)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("provider", "solcast"),
        ("source_kind", "estimated_actuals"),
        ("issued_at", START.isoformat()),
        ("collected_at", None),
        ("collected_at", "not-a-date"),
        ("apikey", "private-key"),
    ],
)
def test_saved_weather_rejects_invalid_provenance_and_timestamps(key: str, value: object) -> None:
    saved = serialize_weather(normalize(weather_payload()))
    saved[key] = value
    with pytest.raises(WeatherFailure, match="schema"):
        restore_weather(saved)
