from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
from astral import Observer, refraction_at_zenith
from astral.sun import zenith

from pyracast.irradiance import local_day_bounds
from pyracast.solar import DAYLIGHT_POLICY, daylight_window

# Astral's sunrise convention: upper solar limb on the sea-level horizon, with refraction.
HORIZON_ZENITH = 90 + 32 / 120 + refraction_at_zenith(90 + 32 / 120)
TURKEY_DIGESTS = {
    (36.0, 26.0): "67f510b333291704130acdcc38d358ade5867cbbe91e3304c24addebf1fbbb67",
    (42.0, 44.0): "fda713d08caed8622cdf6429e14802c3b559d7b7e6bc23e97c82384a30f252ba",
    (41.0082, 28.9784): "d251c42f6971c372fb7aba9aa75a39548995e89e95ae2d319d06fc99284bdf80",
    (39.9334, 32.8597): "aee470bdb6505d00e3c661cd138fb5fd2bdf266e95c942c9bc70b25f2ffc5c73",
    (37.0, 35.3213): "240667890325062198f1c658969e3107e872a5bed11f04b6fcf23ef33cc1297f",
    (38.4237, 44.0): "594d06f1a2f7f13fe96fba7d04637342c0396947328299d10944caa63f102b21",
}


def sun_is_up(observer: Observer, moment: datetime) -> bool:
    return zenith(observer, moment, with_refraction=False) < HORIZON_ZENITH


def horizon_crossings(
    observer: Observer, start: datetime, end: datetime, step: timedelta = timedelta(minutes=2)
) -> list[datetime]:
    crossings = []
    left, up = start, sun_is_up(observer, start)
    while left < end:
        right = min(left + step, end)
        if sun_is_up(observer, right) != up:
            low, high = left, right
            while high - low > timedelta(seconds=1):
                middle = low + (high - low) / 2
                low, high = (middle, high) if sun_is_up(observer, middle) == up else (low, middle)
            crossings.append(high)
            up = not up
        left = right
    return [moment for moment in crossings if start < moment < end]


@pytest.mark.parametrize(("latitude", "longitude"), list(TURKEY_DIGESTS))
def test_turkey_grid_daylight_is_bit_identical(latitude: float, longitude: float) -> None:
    digest = sha256()
    day = date(2026, 1, 1)
    while day.year == 2026:
        result = daylight_window(day, "Europe/Istanbul", latitude, longitude)
        digest.update(
            repr((day, result.windows, result.sunrise, result.sunset, result.status)).encode()
        )
        day += timedelta(days=1)
    assert digest.hexdigest() == TURKEY_DIGESTS[latitude, longitude]


@pytest.mark.parametrize(
    ("day", "zone", "latitude", "longitude"),
    [
        (date(2026, 5, 8), "America/New_York", 40.7128, -74.006),
        (date(2026, 8, 10), "America/New_York", 40.7128, -74.006),
        (date(2026, 5, 17), "Asia/Kolkata", 28.6, 77.2),
        (date(2026, 5, 14), "Europe/Oslo", 69.65, 18.96),
    ],
)
def test_every_horizon_crossing_is_found(
    day: date, zone: str, latitude: float, longitude: float
) -> None:
    start, end = local_day_bounds(day, zone)
    result = daylight_window(day, zone, latitude, longitude)
    crossings = horizon_crossings(Observer(latitude, longitude, elevation=0.0), start, end)
    boundaries = sorted(
        moment for window in result.windows for moment in window if moment not in (start, end)
    )
    assert result.status == "normal"
    assert len(result.windows) == 1
    assert len(boundaries) == len(crossings) == 2
    for boundary, crossing in zip(boundaries, crossings, strict=True):
        assert abs((boundary - crossing).total_seconds()) < 60
    assert result.windows == ((result.sunrise, result.sunset),)


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
