from collections.abc import Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from math import isfinite
from typing import Literal

from astral import Observer, refraction_at_zenith
from astral.sun import sunrise, sunset, zenith

from pyracast.irradiance import local_day_bounds

DAYLIGHT_POLICY = "astral-3.2-sea-level-v1"
_SOLAR_RADIUS = 32 / 120
_HORIZON_ZENITH = 90 + _SOLAR_RADIUS + refraction_at_zenith(90 + _SOLAR_RADIUS)
_SCAN_STEP = timedelta(minutes=30)
_MATCH_SLACK = timedelta(seconds=60)
_CROSSING_PRECISION = timedelta(milliseconds=1)
# Degrees per second: Earth's rotation moves the solar altitude by at most 15 degrees per hour
# and 16 covers the declination drift. Astral's zenith is constant within a whole second, so
# a span is compared with one second more travel and never split below one second.
_ALTITUDE_RATE = 16 / 3600
_FINEST_SPAN = timedelta(seconds=1)

type Event = tuple[datetime, bool]


@dataclass(frozen=True)
class Daylight:
    windows: tuple[tuple[datetime, datetime], ...]
    sunrise: datetime | None
    sunset: datetime | None
    status: Literal["normal", "polar_day", "polar_night"]


type TimeWindows = tuple[tuple[datetime, datetime], ...]


def daylight_energy_windows(
    daylight: Daylight, native_bounds: Iterable[tuple[datetime, datetime]]
) -> TimeWindows:
    selected: list[tuple[datetime, datetime]] = []
    for begin, stop in sorted(
        (left.astimezone(UTC), right.astimezone(UTC)) for left, right in native_bounds
    ):
        if not any(begin < end and stop > start for start, end in daylight.windows):
            continue
        if selected and begin <= selected[-1][1]:
            selected[-1] = (selected[-1][0], max(stop, selected[-1][1]))
        else:
            selected.append((begin, stop))
    return tuple(selected)


def _daylight_overlap(daylight: Daylight, begin: datetime, stop: datetime) -> timedelta:
    if begin >= stop:
        return timedelta()
    return sum(
        (
            min(stop, right) - max(begin, left)
            for left, right in daylight.windows
            if begin < right and stop > left
        ),
        timedelta(),
    )


def summarize_daylight(
    daylight: Daylight,
    intervals: Iterable[tuple[datetime, datetime, float | None]],
    *,
    start: datetime,
    end: datetime,
    energy_windows: TimeWindows,
    unit: Literal["mwh", "kwh_m2"],
    input_windows: Iterable[tuple[datetime, datetime]] | None = None,
) -> dict[str, object]:
    """Integrate validated native means, retaining complete daylight boundary intervals."""
    coverage = valid_coverage = energy_coverage = timedelta()
    energy = 0.0
    scale = 3600 if unit == "mwh" else 3_600_000
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    for begin, stop, value in intervals:
        begin, stop = max(start, begin.astimezone(UTC)), min(end, stop.astimezone(UTC))
        if begin >= stop:
            continue
        overlap = _daylight_overlap(daylight, begin, stop)
        coverage += overlap
        if value is None:
            continue
        valid_coverage += overlap
        for left, right in energy_windows:
            if begin < right and stop > left:
                covered = min(stop, right) - max(begin, left)
                energy_coverage += covered
                energy += value * (covered.total_seconds() / scale)
    duration = sum((right - left for left, right in daylight.windows), timedelta())
    energy_duration = sum(
        (
            min(end, right) - max(start, left)
            for left, right in energy_windows
            if left < end and right > start
        ),
        timedelta(),
    )
    complete = timedelta() < duration == valid_coverage and energy_coverage == energy_duration
    if not isfinite(energy):
        raise ValueError("Daylight energy must be finite")
    result: dict[str, object] = {
        "policy": DAYLIGHT_POLICY,
        "energy_scope": "whole_native_intervals",
        "status": daylight.status,
        "sunrise": daylight.sunrise.isoformat() if daylight.sunrise is not None else None,
        "sunset": daylight.sunset.isoformat() if daylight.sunset is not None else None,
        "windows": [
            {"start": left.isoformat(), "end": right.isoformat()}
            for left, right in daylight.windows
        ],
        "duration_seconds": duration.total_seconds(),
        "coverage_seconds": coverage.total_seconds(),
        "valid_coverage_seconds": valid_coverage.total_seconds(),
        "coverage_fraction": valid_coverage / duration if duration else None,
        "complete": complete,
        f"energy_{unit}": energy if complete else None,
        f"partial_energy_{unit}": energy,
    }
    if input_windows is not None:
        input_coverage = sum(
            (
                _daylight_overlap(daylight, max(start, begin), min(end, stop))
                for begin, stop in daylight_energy_windows(daylight, input_windows)
            ),
            timedelta(),
        )
        result["input_coverage_seconds"] = input_coverage.total_seconds()
        result["input_coverage_fraction"] = input_coverage / duration if duration else None
    return result


def daylight_window(day: date, timezone: str, latitude: float, longitude: float) -> Daylight:
    """Use Astral's sea-level horizon; terrain and local obstructions are not modeled."""
    if not isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("Latitude must be finite and between -90 and 90 degrees")
    if not isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValueError("Longitude must be finite and between -180 and 180 degrees")
    start, end = local_day_bounds(day, timezone)
    observer = Observer(latitude, longitude, elevation=0.0)
    return daylight_from_events(
        _horizon_events(observer, start, end),
        start,
        end,
        sun_up=_sun_is_up(observer, start + (end - start) / 2),
    )


def _height(observer: Observer, moment: datetime) -> float:
    """Degrees above the horizon test; positive exactly while the sun is up."""
    return _HORIZON_ZENITH - zenith(observer, moment, with_refraction=False)


def _sun_is_up(observer: Observer, moment: datetime) -> bool:
    return _height(observer, moment) > 0


def _astral_events(observer: Observer, start: datetime, end: datetime) -> list[Event]:
    events: set[Event] = set()
    cursor = start.date() - timedelta(days=1)
    while cursor <= end.date() + timedelta(days=1):
        for calculate, rising in ((sunrise, True), (sunset, False)):
            # Astral searches per UTC date and raises when that date holds no such event.
            with suppress(ValueError):
                events.add((calculate(observer, cursor, UTC), rising))
        cursor += timedelta(days=1)
    return sorted(events)


def _bisect_crossing(observer: Observer, left: datetime, right: datetime) -> datetime:
    up = _sun_is_up(observer, left)
    while right - left > _CROSSING_PRECISION:
        middle = left + (right - left) / 2
        left, right = (middle, right) if _sun_is_up(observer, middle) == up else (left, middle)
    return right


def _brackets(
    observer: Observer, left: datetime, right: datetime, low: float, high: float
) -> Iterator[tuple[datetime, datetime, bool]]:
    """Yield (left, right, rising) around each crossing in [left, right], given both heights.

    A span whose ends show one state is halved while the sun could reach the horizon and come
    back inside it (|low| + |high| degrees of travel), so only a dip of at most a second is lost.
    """
    if (low > 0) != (high > 0):
        yield left, right, high > 0
        return
    span = right - left
    if span <= _FINEST_SPAN or abs(low) + abs(high) > _ALTITUDE_RATE * (span.total_seconds() + 1):
        return
    middle = left + span / 2
    height = _height(observer, middle)
    yield from _brackets(observer, left, middle, low, height)
    yield from _brackets(observer, middle, right, height, high)


def _horizon_events(observer: Observer, start: datetime, end: datetime) -> list[Event]:
    # Astral's event is kept wherever a bracket claims it, so its timings stay unchanged; the
    # scan finds the crossings Astral misses near 00:00 UTC or inside one step. The scan runs
    # one step beyond both ends of the day so that an event and its crossing on opposite sides
    # of a day boundary still meet; the caller keeps the events inside the day.
    unmatched = _astral_events(observer, start, end)
    moments = [start - _SCAN_STEP]
    while moments[-1] < end:
        moments.append(min(moments[-1] + _SCAN_STEP, end))
    moments.append(end + _SCAN_STEP)
    samples = [(moment, _height(observer, moment)) for moment in moments]
    return sorted(
        _claim(observer, unmatched, *bracket)
        for (left, low), (right, high) in pairwise(samples)
        for bracket in _brackets(observer, left, right, low, high)
    )


def _claim(
    observer: Observer, unmatched: list[Event], left: datetime, right: datetime, rising: bool
) -> Event:
    # Near the poles the sun grazes the horizon and Astral's event can sit minutes from the
    # crossing, so an event is kept only where the horizon test flips within the slack. Astral
    # can report one crossing for two UTC dates a fraction of a second apart; the nearer wins.
    match = min(
        (
            event
            for event in unmatched
            if event[1] == rising
            and left - _MATCH_SLACK <= event[0] <= right + _MATCH_SLACK
            and _sun_is_up(observer, event[0] - _MATCH_SLACK) != rising
            and _sun_is_up(observer, event[0] + _MATCH_SLACK) == rising
        ),
        key=lambda event: max(left - event[0], event[0] - right, timedelta()),
        default=None,
    )
    if match is None:
        return _bisect_crossing(observer, left, right), rising
    unmatched.remove(match)
    return match


def daylight_from_events(
    events: Iterable[Event], start: datetime, end: datetime, *, sun_up: bool
) -> Daylight:
    """Assemble the half-open local day; `sun_up` decides a day without events."""
    ordered = sorted(event for event in events if start <= event[0] < end)
    if not ordered:
        return Daylight(
            ((start, end),) if sun_up else (),
            None,
            None,
            "polar_day" if sun_up else "polar_night",
        )
    previous = start
    windows = []
    rises, sets = [], []
    for event, rising in ordered:
        if rising:
            rises.append(event)
        else:
            sets.append(event)
            if previous < event:
                windows.append((previous, event))
        previous = event
    if ordered[-1][1]:
        windows.append((previous, end))
    return Daylight(
        tuple(windows), rises[0] if rises else None, sets[-1] if sets else None, "normal"
    )
