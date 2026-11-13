import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from pyracast.irradiance.weather_reference import (
    ATTRIBUTION,
    summarize_weather,
    unavailable_weather,
)
from pyracast.weather import WeatherData, WeatherInterval


def interval(start: datetime, ghi: float | None = 100.0) -> WeatherInterval:
    return WeatherInterval(start, start + timedelta(hours=1), ghi, None, 5.0, None, 0.0, 0.0)


def data(start: datetime, count: int) -> WeatherData:
    return WeatherData(
        raw_payload={"latitude": 50.1, "longitude": 10.2},
        intervals=tuple(interval(start + timedelta(hours=i)) for i in range(count)),
        collected_at=start,
        requested_latitude=50.0,
        requested_longitude=10.0,
        requested_start=start,
        requested_end=start + timedelta(days=2),
    )


@pytest.mark.parametrize(
    ("day", "start", "hours"),
    [
        (date(2026, 3, 29), datetime(2026, 3, 28, 23, tzinfo=UTC), 23),
        (date(2026, 10, 25), datetime(2026, 10, 24, 22, tzinfo=UTC), 25),
    ],
)
def test_dst_days_integrate_elapsed_utc(day: date, start: datetime, hours: int) -> None:
    result = summarize_weather(data(start, hours), day, "Europe/Berlin")
    assert result["complete"] is True
    assert result["energy_kwh_m2"] == pytest.approx(hours * 0.1)
    assert result["coverage_seconds"] == hours * 3600
    assert result["coverage_fraction"] == 1
    json.dumps(result, allow_nan=False)


def test_fractional_offset_keeps_native_intervals() -> None:
    start = datetime(2025, 12, 31, 18, tzinfo=UTC)
    result = summarize_weather(data(start, 25), date(2026, 1, 1), "Asia/Kathmandu")
    assert result["energy_kwh_m2"] == pytest.approx(2.4)
    flags = result["quality_flags"]
    assert isinstance(flags, list)
    assert "uniform_mean_proration" in flags
    rows = result["intervals"]
    assert isinstance(rows, list)
    assert len(rows) == 25
    assert rows[0]["start"] == start.isoformat()
    assert rows[0]["overlap_seconds"] == 2700
    assert rows[-1]["overlap_seconds"] == 900


def test_gaps_nulls_and_zero_are_distinct() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    source = data(start, 24)
    source = replace(
        source, intervals=(interval(start, 0), interval(start + timedelta(hours=2), None))
    )
    result = summarize_weather(source, start.date(), "UTC")
    assert result["status"] == "partial"
    assert result["energy_kwh_m2"] is None
    assert result["partial_energy_kwh_m2"] == 0
    assert result["coverage_seconds"] == 7200
    assert result["valid_coverage_seconds"] == 3600
    assert result["coverage_fraction"] == pytest.approx(1 / 24)
    empty = summarize_weather(replace(source, intervals=()), start.date(), "UTC")
    assert empty["coverage_fraction"] == 0
    assert empty["complete"] is False


def test_finite_irradiance_does_not_overflow_during_unit_conversion() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    source = data(start, 24)
    source = replace(
        source, intervals=tuple(replace(row, ghi_wm2=1e306) for row in source.intervals)
    )
    result = summarize_weather(source, start.date(), "UTC")
    assert result["energy_kwh_m2"] == pytest.approx(2.4e304)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "kind", ["duplicate", "unsorted", "naive", "negative", "nonfinite", "text"]
)
def test_invalid_intervals_are_rejected(kind: str) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = interval(start)
    intervals: tuple[WeatherInterval, ...]
    if kind == "duplicate":
        intervals = (first, first)
    elif kind == "unsorted":
        intervals = (interval(start + timedelta(hours=1)), first)
    elif kind == "naive":
        intervals = (interval(start.replace(tzinfo=None)),)
    elif kind == "negative":
        intervals = (interval(start, -1),)
    elif kind == "nonfinite":
        intervals = (interval(start, float("nan")),)
    else:
        intervals = (replace(first, ghi_wm2="bad"),)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"Weather|weather"):
        summarize_weather(replace(data(start, 1), intervals=intervals), start.date(), "UTC")


def test_other_invalid_metadata_and_intervals() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    source = data(start, 1)
    with pytest.raises(ValueError, match="local date"):
        summarize_weather(source, date(2011, 12, 30), "Pacific/Apia")
    with pytest.raises(ValueError, match="requested window"):
        summarize_weather(replace(source, requested_end=start), start.date(), "UTC")
    with pytest.raises(ValueError, match="issue time"):
        summarize_weather(replace(source, issued_at=start), start.date(), "UTC")
    with pytest.raises(ValueError, match="one hour"):
        summarize_weather(
            replace(source, intervals=(replace(source.intervals[0], end=start),)),
            start.date(),
            "UTC",
        )
    with pytest.raises(ValueError, match="weather value"):
        summarize_weather(
            replace(source, intervals=(replace(source.intervals[0], ghi_wm2=10**1000),)),
            start.date(),
            "UTC",
        )
    outside = replace(source, intervals=(interval(start - timedelta(hours=2)),))
    assert summarize_weather(outside, start.date(), "UTC")["intervals"] == []


def test_unavailable_preserves_attribution() -> None:
    result = unavailable_weather("weather_request_failed")
    assert result["status"] == "unavailable"
    assert result["quality_flags"] == ["external_weather_unavailable", "weather_request_failed"]
    assert result["attribution"] == ATTRIBUTION
    assert result["attribution"] is not ATTRIBUTION
