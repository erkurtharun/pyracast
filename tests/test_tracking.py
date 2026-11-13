from datetime import UTC, datetime, timedelta

import pytest

from pyracast.connections.models import SiteBinding
from pyracast.plants.models import CalibrationRun, PlantConfiguration
from pyracast.plants.numerics import MODEL_VERSION
from pyracast.plants.tracking import MODEL_VERSION as TRACKING_MODEL_VERSION
from pyracast.plants.tracking import interval_gain, tracking_gain
from pyracast.plants.transfer import (
    calibration_matches_transfer,
    reference_power_mw,
    tracking_modeled,
    transfer_model,
)

LATITUDE, LONGITUDE = 37.80375, 34.460913


# Expected values come from an independent pvlib 0.16.1 chain: Erbs decomposition,
# singleaxis(gcr=0.4, max_angle=55, backtrack=True) and Hay-Davies transposition.
@pytest.mark.parametrize(
    ("reference", "zenith", "azimuth", "day_of_year", "expected"),
    [
        (0.4738, 58.35, 83.49, 172, 1.7547),
        (0.1699, 70.37, 284.97, 172, 1.2285),
        (0.2210, 73.77, 135.86, 10, 1.9169),
        (0.4795, 60.00, 186.79, 10, 1.0191),
        (0.3051, 65.86, 249.66, 79, 1.7326),
        (0.1255, 33.17, 138.60, 108, 0.9691),
    ],
)
def test_tracking_gain_matches_independent_reference_chain(
    reference: float, zenith: float, azimuth: float, day_of_year: int, expected: float
) -> None:
    assert tracking_gain(reference, zenith, azimuth, day_of_year) == pytest.approx(
        expected, abs=0.012
    )


def test_tracker_is_flat_at_solar_noon_and_never_adjusts_night_or_low_sun() -> None:
    assert tracking_gain(0.7678, 14.36, 180.0, 172) == pytest.approx(1.0, abs=1e-3)
    assert tracking_gain(0.0, 40.0, 120.0, 172) == 1.0
    assert tracking_gain(0.2, 87.0, 100.0, 172) == 1.0
    assert tracking_gain(0.0005, 60.0, 120.0, 172) == 1.0


def test_gain_grows_with_clearness_and_loses_diffuse_light_under_overcast() -> None:
    overcast = tracking_gain(0.04, 60.0, 110.0, 80)
    clear = tracking_gain(0.42, 60.0, 110.0, 80)
    very_clear = tracking_gain(0.6, 60.0, 110.0, 80)
    assert 0.75 < overcast < 1.0
    assert clear > 1.5
    assert very_clear > 1.2


def test_backtracking_limits_the_low_sun_gain() -> None:
    low = tracking_gain(0.05, 84.0, 95.0, 172)
    assert 0.5 < low < 3.0


def test_interval_gain_uses_the_interval_midpoint_solar_position() -> None:
    start = datetime(2026, 6, 21, 5, 0, tzinfo=UTC)
    gain = interval_gain(start, start + timedelta(minutes=30), 0.4738, LATITUDE, LONGITUDE)
    assert gain == pytest.approx(1.7547, abs=0.012)
    mirrored = interval_gain(
        start + timedelta(hours=9),
        start + timedelta(hours=9, minutes=30),
        0.4738,
        LATITUDE,
        LONGITUDE,
    )
    assert mirrored == pytest.approx(gain, abs=0.08)


def _configuration(**binding: object) -> PlantConfiguration:
    values: dict[str, object] = {
        "capacity_kw": 1000.0,
        "dc_capacity_kw": 1000.0,
        "tilt_degrees": 0.0,
        "losses_percent": 0.0,
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "timezone": "Europe/Istanbul",
    }
    return PlantConfiguration(
        binding=SiteBinding(**(values | binding)), dc_capacity_mw=130.0, mounting_type="tracking"
    )


@pytest.mark.parametrize(
    "binding",
    [
        {"tilt_degrees": 20.0},
        {"dc_capacity_kw": None},
        {"dc_capacity_kw": 0.0},
        {"losses_percent": 100.0},
    ],
)
def test_geometry_requires_a_horizontal_reference_with_known_dc_capacity(
    binding: dict[str, object],
) -> None:
    configuration = _configuration(**binding)
    start = datetime(2026, 6, 21, 5, 0, tzinfo=UTC)
    assert not tracking_modeled(configuration)
    assert transfer_model(configuration) == MODEL_VERSION
    assert reference_power_mw(
        configuration, start, start + timedelta(minutes=30), 473.8
    ) == pytest.approx(0.4738 * 130)


def test_non_tracking_configurations_keep_the_direct_reference_transfer() -> None:
    configuration = _configuration()
    configuration.mounting_type = "fixed"
    start = datetime(2026, 6, 21, 5, 0, tzinfo=UTC)
    assert transfer_model(configuration) == MODEL_VERSION
    assert reference_power_mw(
        configuration, start, start + timedelta(minutes=30), 473.8
    ) == pytest.approx(0.4738 * 130)


def test_tracking_transfer_applies_the_gain_to_loss_free_reference_power() -> None:
    start = datetime(2026, 6, 21, 5, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    configuration = _configuration()
    assert tracking_modeled(configuration)
    assert transfer_model(configuration) == TRACKING_MODEL_VERSION
    power = reference_power_mw(configuration, start, end, 473.8)
    assert power == pytest.approx(0.4738 * 130 * 1.7547, rel=0.01)
    lossy = _configuration(losses_percent=10.0)
    assert reference_power_mw(lossy, start, end, 473.8 * 0.9) == pytest.approx(power * 0.9)


def test_calibrations_apply_only_under_the_transfer_model_that_fitted_them() -> None:
    tracking, fixed = _configuration(), _configuration()
    fixed.mounting_type = "fixed"
    legacy = CalibrationRun(result={})
    current = CalibrationRun(result={"transfer_model": TRACKING_MODEL_VERSION})
    assert calibration_matches_transfer(legacy, fixed)
    assert not calibration_matches_transfer(legacy, tracking)
    assert calibration_matches_transfer(current, tracking)
    assert not calibration_matches_transfer(current, fixed)
    assert not calibration_matches_transfer(CalibrationRun(result={"transfer_model": 1}), fixed)
