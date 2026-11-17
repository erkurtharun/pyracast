from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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


def _sun_is_up(observer: Observer, moment: datetime) -> bool:
    return zenith(observer, moment, with_refraction=False) < _HORIZON_ZENITH


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


def _hidden_pairs(unmatched: list[Event]) -> list[Event]:
    # Both samples around an unmatched event show one state, so it is a crossing only together
    # with an opposite event inside the same step.
    return [
        event
        for event in unmatched
        if any(
            other[1] != event[1] and abs(other[0] - event[0]) < _SCAN_STEP for other in unmatched
        )
    ]


def _horizon_events(observer: Observer, start: datetime, end: datetime) -> list[Event]:
    # Astral's event is kept wherever the scan brackets one, so its timings stay unchanged; the
    # scan fills the crossings Astral misses near 00:00 UTC. Hazard: a daylight or night shorter
    # than the scan step hides between samples; only Astral's set-and-rise pairs recover it.
    unmatched = _astral_events(observer, start, end)
    events: list[Event] = []
    left, up = start, _sun_is_up(observer, start)
    while left < end:
        right = min(left + _SCAN_STEP, end)
        if _sun_is_up(observer, right) != up:
            up = not up
            match = next(
                (
                    event
                    for event in unmatched
                    if event[1] == up and left - _MATCH_SLACK <= event[0] <= right + _MATCH_SLACK
                ),
                None,
            )
            if match is None:
                events.append((_bisect_crossing(observer, left, right), up))
            else:
                unmatched.remove(match)
                events.append(match)
        left = right
    return sorted(events + _hidden_pairs(unmatched))


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
