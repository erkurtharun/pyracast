from datetime import UTC, datetime
from math import acos, atan2, cos, degrees, pi, radians, sin, sqrt

from astral import Observer
from astral.sun import zenith_and_azimuth

MODEL_VERSION = "tracking-geometry-transfer-v1"
# Assumed north-south horizontal axis with slope-free backtracking; not declared plant geometry.
GROUND_COVERAGE_RATIO = 0.40
MAX_ROTATION_DEGREES = 55.0
ALBEDO = 0.20
# reference_fraction = LINEAR * g + QUADRATIC * g**2, g = GHI / 1000 W/m2, fitted on the
# authorized horizontal loss-free reference case; it only sets the clearness index.
REFERENCE_LINEAR = 0.9964
REFERENCE_QUADRATIC = -0.1484
# Power change per W/m2 of plane irradiance above horizontal: -0.35 %/K at 29 W/m2/K.
RELATIVE_TEMPERATURE_PER_WM2 = -0.0035 / 29.0
MAX_ZENITH_DEGREES = 87.0
MIN_GHI_WM2 = 1.0
MAX_GAIN = 6.0
SOLAR_CONSTANT_WM2 = 1361.0
_MIN_COS_ZENITH = 0.065


def _extraterrestrial(day_of_year: int) -> float:
    angle = 2 * pi * (day_of_year - 1) / 365
    return SOLAR_CONSTANT_WM2 * (
        1.00011
        + 0.034221 * cos(angle)
        + 0.00128 * sin(angle)
        + 0.000719 * cos(2 * angle)
        + 0.000077 * sin(2 * angle)
    )


def _diffuse_fraction(clearness: float) -> float:
    """Erbs, Klein and Duffie (1982)."""
    if clearness <= 0.22:
        return 1 - 0.09 * clearness
    if clearness <= 0.8:
        return (
            0.9511
            - 0.1604 * clearness
            + 4.388 * clearness**2
            - 16.638 * clearness**3
            + 12.336 * clearness**4
        )
    return 0.165


def _rotation(across_axis: float, vertical: float) -> float:
    rotation = degrees(atan2(across_axis, vertical))
    shading_limit = abs(cos(radians(rotation))) / GROUND_COVERAGE_RATIO
    if shading_limit < 1:
        correction = degrees(acos(shading_limit))
        rotation += -correction if rotation >= 0 else correction
    return max(-MAX_ROTATION_DEGREES, min(MAX_ROTATION_DEGREES, rotation))


def tracking_gain(
    reference_fraction: float, zenith: float, azimuth: float, day_of_year: int
) -> float:
    """Ratio of tracker plane output to horizontal output for one interval mean.

    `reference_fraction` is loss-free horizontal reference power per unit DC capacity;
    angles are degrees with azimuth clockwise from north. Low sun returns 1.
    """
    cos_zenith = cos(radians(zenith))
    if reference_fraction <= 0 or zenith >= MAX_ZENITH_DEGREES:
        return 1.0
    discriminant = REFERENCE_LINEAR**2 + 4 * REFERENCE_QUADRATIC * reference_fraction
    ghi = 1000 * (sqrt(max(discriminant, 0.0)) - REFERENCE_LINEAR) / (2 * REFERENCE_QUADRATIC)
    if ghi <= MIN_GHI_WM2:
        return 1.0
    extraterrestrial = _extraterrestrial(day_of_year)
    clearness = min(ghi / (extraterrestrial * max(cos_zenith, _MIN_COS_ZENITH)), 1.0)
    dhi = ghi * _diffuse_fraction(clearness)
    dni = (ghi - dhi) / cos_zenith
    across_axis = sin(radians(zenith)) * sin(radians(azimuth - 180))
    rotation = radians(_rotation(across_axis, cos_zenith))
    cos_incidence = max(across_axis * sin(rotation) + cos_zenith * cos(rotation), 0.0)
    anisotropy = min(dni / extraterrestrial, 1.0)
    sky_view = (1 + cos(rotation)) / 2
    plane = (
        dni * cos_incidence
        + dhi * (anisotropy * cos_incidence / cos_zenith + (1 - anisotropy) * sky_view)
        + ghi * ALBEDO * (1 - sky_view)
    )
    excess = max(-200.0, min(600.0, plane - ghi))
    return min(plane / ghi, MAX_GAIN) * (1 + RELATIVE_TEMPERATURE_PER_WM2 * excess)


def interval_gain(
    start: datetime, end: datetime, reference_fraction: float, latitude: float, longitude: float
) -> float:
    """Evaluate the gain at the interval midpoint with a sea-level observer."""
    midpoint = (start + (end - start) / 2).astimezone(UTC)
    zenith, azimuth = zenith_and_azimuth(Observer(latitude, longitude, 0.0), midpoint)
    return tracking_gain(reference_fraction, zenith, azimuth, midpoint.timetuple().tm_yday)
