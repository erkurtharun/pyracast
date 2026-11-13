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
_POLAR_ERRORS = {
    "Sun is always below the horizon on this day, at this location.",
    "Sun is always above the horizon on this day, at this location.",
}


@dataclass(frozen=True)
class Daylight:
    windows: tuple[tuple[datetime, datetime], ...]
    sunrise: datetime | None
    sunset: datetime | None
    status: Literal["normal", "polar_day", "polar_night"]


def daylight_window(day: date, timezone: str, latitude: float, longitude: float) -> Daylight:
    """Use Astral's sea-level horizon; terrain and local obstructions are not modeled."""
    if not isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("Latitude must be finite and between -90 and 90 degrees")
    if not isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValueError("Longitude must be finite and between -180 and 180 degrees")
    start, end = local_day_bounds(day, timezone)
    observer = Observer(latitude, longitude, elevation=0.0)
    events: set[tuple[datetime, bool]] = set()
    cursor = start.date() - timedelta(days=1)
    last = end.date() + timedelta(days=1)
    while cursor <= last:
        for calculate, rising in ((sunrise, True), (sunset, False)):
            try:
                event = calculate(observer, cursor, UTC)
            except ValueError as exc:
                # Python 3.15 changed the acos error that Astral 3.2 translates for polar dates.
                if str(exc) not in _POLAR_ERRORS and not str(exc).startswith(
                    "expected a number in range from -1 up to 1, got "
                ):
                    raise
            else:
                if start <= event < end:
                    events.add((event, rising))
        cursor += timedelta(days=1)
    ordered = sorted(events)
    if not ordered:
        daylight = (
            zenith(observer, start + (end - start) / 2, with_refraction=False) < _HORIZON_ZENITH
        )
        return Daylight(
            ((start, end),) if daylight else (),
            None,
            None,
            "polar_day" if daylight else "polar_night",
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
