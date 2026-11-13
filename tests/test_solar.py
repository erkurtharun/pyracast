from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
from astral import Observer

from pyracast.solar import DAYLIGHT_POLICY, daylight_window


def test_published_observatory_reference_and_immutable_policy() -> None:
    # Griffith Observatory's USNO MICA table: 2025-03-20, rise 06:56 PDT, set 19:05 PDT.
    # https://griffithobservatory.lacity.gov/explore/observing-the-sky/whats-in-the-sky/the-sun/2025-sunrise-and-sunset/
    result = daylight_window(date(2025, 3, 20), "America/Los_Angeles", 34.118333, -118.298333)
    assert DAYLIGHT_POLICY == "astral-3.2-sea-level-v1"
    assert result.status == "normal"
    assert result.sunrise is not None
    assert result.sunset is not None
    assert abs((result.sunrise - datetime(2025, 3, 20, 13, 56, tzinfo=UTC)).total_seconds()) < 60
    assert abs((result.sunset - datetime(2025, 3, 21, 2, 5, tzinfo=UTC)).total_seconds()) < 60
    assert result.windows == ((result.sunrise, result.sunset),)
    with pytest.raises(FrozenInstanceError):
        result.__setattr__("status", "polar_night")


@pytest.mark.parametrize(
    ("day", "start", "end", "offset_hours"),
    [
        (
            date(2026, 3, 29),
            datetime(2026, 3, 28, 23, tzinfo=UTC),
            datetime(2026, 3, 29, 22, tzinfo=UTC),
            2,
        ),
        (
            date(2026, 10, 25),
            datetime(2026, 10, 24, 22, tzinfo=UTC),
            datetime(2026, 10, 25, 23, tzinfo=UTC),
            1,
        ),
    ],
)
def test_dst_uses_elapsed_utc_bounds(
    day: date, start: datetime, end: datetime, offset_hours: int
) -> None:
    result = daylight_window(day, "Europe/Berlin", 52.52, 13.405)
    assert result.status == "normal"
    assert len(result.windows) == 1
    rise, setting = result.windows[0]
    assert start < rise < setting < end
    assert rise.tzinfo is UTC
    assert setting.tzinfo is UTC
    assert rise.astimezone(ZoneInfo("Europe/Berlin")).utcoffset() == timedelta(hours=offset_hours)


@pytest.mark.parametrize(
    ("zone", "latitude", "longitude", "start", "end"),
    [
        (
            "Pacific/Kiritimati",
            1.8721,
            -157.4278,
            datetime(2026, 3, 19, 10, tzinfo=UTC),
            datetime(2026, 3, 20, 10, tzinfo=UTC),
        ),
        (
            "Etc/GMT+12",
            0,
            -180,
            datetime(2026, 3, 20, 12, tzinfo=UTC),
            datetime(2026, 3, 21, 12, tzinfo=UTC),
        ),
    ],
)
def test_date_line_preserves_selected_local_day(
    zone: str, latitude: float, longitude: float, start: datetime, end: datetime
) -> None:
    result = daylight_window(date(2026, 3, 20), zone, latitude, longitude)
    assert result.status == "normal"
    assert len(result.windows) == 1
    rise, setting = result.windows[0]
    assert start < rise < setting < end
    assert rise.astimezone(ZoneInfo(zone)).date() == date(2026, 3, 20)
    assert setting.astimezone(ZoneInfo(zone)).date() == date(2026, 3, 20)


def test_daylight_crossing_midnight_has_two_windows() -> None:
    result = daylight_window(date(2026, 6, 21), "UTC", 65, 25)
    assert result.status == "normal"
    assert len(result.windows) == 2
    assert result.sunrise is not None
    assert result.sunset is not None
    assert result.sunset < result.sunrise
    assert result.windows == (
        (datetime(2026, 6, 21, tzinfo=UTC), result.sunset),
        (result.sunrise, datetime(2026, 6, 22, tzinfo=UTC)),
    )


@pytest.mark.parametrize("day", [date(2026, 5, 14), date(2026, 5, 17)])
def test_polar_transition_can_have_only_one_event(day: date) -> None:
    result = daylight_window(day, "Europe/Oslo", 69.65, 18.96)
    assert result.status == "normal"
    assert len(result.windows) == 1
    if day.day == 14:
        assert result.sunrise is None
        assert result.windows == ((datetime(2026, 5, 13, 22, tzinfo=UTC), result.sunset),)
    else:
        assert result.sunset is None
        assert result.windows == ((result.sunrise, datetime(2026, 5, 17, 22, tzinfo=UTC)),)


@pytest.mark.parametrize("latitude", [69.65, 90])
def test_polar_day_is_the_entire_elapsed_local_day(latitude: float) -> None:
    result = daylight_window(date(2026, 6, 21), "Europe/Oslo", latitude, 18.96)
    assert result.status == "polar_day"
    assert result.sunrise is None
    assert result.sunset is None
    assert result.windows == (
        (datetime(2026, 6, 20, 22, tzinfo=UTC), datetime(2026, 6, 21, 22, tzinfo=UTC)),
    )


@pytest.mark.parametrize(("latitude", "longitude"), [(69.65, 18.96), (90, 180), (-90, -180)])
def test_polar_night_has_no_required_meter_intervals(latitude: float, longitude: float) -> None:
    day = date(2026, 12, 21) if latitude > 0 else date(2026, 6, 21)
    result = daylight_window(day, "UTC", latitude, longitude)
    assert result.status == "polar_night"
    assert result.windows == ()
    assert result.sunrise is None
    assert result.sunset is None


@pytest.mark.parametrize("latitude", [float("nan"), float("inf"), -91, 91])
def test_invalid_latitude_rejected(latitude: float) -> None:
    with pytest.raises(ValueError, match="Latitude"):
        daylight_window(date(2026, 1, 1), "UTC", latitude, 0)


@pytest.mark.parametrize("longitude", [float("nan"), float("inf"), -181, 181])
def test_invalid_longitude_rejected(longitude: float) -> None:
    with pytest.raises(ValueError, match="Longitude"):
        daylight_window(date(2026, 1, 1), "UTC", 0, longitude)


def test_invalid_timezone_and_skipped_local_date_propagate() -> None:
    with pytest.raises(ZoneInfoNotFoundError):
        daylight_window(date(2026, 1, 1), "Invalid/Zone", 0, 0)
    with pytest.raises(ValueError, match="no positive duration"):
        daylight_window(date(2011, 12, 30), "Pacific/Apia", -13.8, -171.8)


@pytest.mark.parametrize(
    "message",
    [
        "Sun is always below the horizon on this day, at this location.",
        "Sun is always above the horizon on this day, at this location.",
    ],
)
def test_astral_translated_polar_errors_are_accepted(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    def no_event(_observer: Observer, _day: date, _zone: tzinfo) -> datetime:
        raise ValueError(message)

    monkeypatch.setattr("pyracast.solar.sunrise", no_event)
    monkeypatch.setattr("pyracast.solar.sunset", no_event)
    result = daylight_window(date(2026, 6, 21), "UTC", 90, 0)
    assert result.status == "polar_day"


@pytest.mark.parametrize(
    "error", [ValueError("unexpected calculation error"), RuntimeError("failed")]
)
def test_unexpected_calculation_failures_propagate(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def broken(_observer: Observer, _day: date, _zone: tzinfo) -> datetime:
        raise error

    monkeypatch.setattr("pyracast.solar.sunrise", broken)
    with pytest.raises(type(error), match=str(error)):
        daylight_window(date(2026, 3, 20), "UTC", 0, 0)


def test_events_exactly_at_midnight_preserve_half_open_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def rise(_observer: Observer, day: date, _zone: tzinfo) -> datetime:
        return datetime.combine(day, time(12), UTC)

    def setting(_observer: Observer, day: date, _zone: tzinfo) -> datetime:
        return datetime.combine(day, time.min, UTC)

    monkeypatch.setattr("pyracast.solar.sunrise", rise)
    monkeypatch.setattr("pyracast.solar.sunset", setting)
    result = daylight_window(date(2026, 3, 20), "UTC", 0, 0)
    assert result.sunset == datetime(2026, 3, 20, tzinfo=UTC)
    assert result.windows == (
        (datetime(2026, 3, 20, 12, tzinfo=UTC), datetime(2026, 3, 21, tzinfo=UTC)),
    )
