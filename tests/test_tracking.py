import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from astral import Observer, sun

from pyracast.connections.models import SiteBinding
from pyracast.plants import tracking
from pyracast.plants.models import CalibrationRun, PlantConfiguration
from pyracast.plants.numerics import MODEL_VERSION
from pyracast.plants.tracking import MODEL_V1, MODEL_V2, interval_gain, tracking_gain
from pyracast.plants.transfer import (
    calibration_matches_transfer,
    reference_power_mw,
    tracking_modeled,
    transfer_model,
)

LATITUDE, LONGITUDE = 37.80375, 34.460913
FIXTURES = Path(__file__).parent / "fixtures"
# Gains of an independent pvlib chain rounded to four decimals; scripts/check_scientific.py
# recomputes and compares them. v1 feeds one zenith to both steps, v2 the true zenith to Erbs
# and the apparent zenith to the tracker geometry.
REFERENCE_CHAIN = json.loads((FIXTURES / "tracking_gain_reference.json").read_text("utf-8"))
REFERENCE_CHAIN_V2 = json.loads((FIXTURES / "tracking_gain_reference_v2.json").read_text("utf-8"))
# Outputs of origin/main 14fa0004 as float.hex, written by running its tracking and transfer
# modules (see docs/IRRADIANCE.md, "Tracking geometry transfer"). Compared to a relative 1e-12
# because the platform libm differs in the last bits.
GOLDEN_V1 = json.loads((FIXTURES / "tracking_v1_golden.json").read_text("utf-8"))
GOLDEN_REL_TOL = 1e-12
assert REFERENCE_CHAIN, "Tracker gain reference table must contain cases."
assert REFERENCE_CHAIN_V2, "Tracker gain v2 reference table must contain cases."


def _matches_golden(actual: float, golden_hex: str) -> bool:
    return math.isclose(actual, float.fromhex(golden_hex), rel_tol=GOLDEN_REL_TOL, abs_tol=1e-300)


@pytest.mark.parametrize("case", REFERENCE_CHAIN)
def test_tracking_gain_matches_independent_reference_chain(case: dict[str, float]) -> None:
    zenith = case["zenith"]
    gain = tracking_gain(
        case["reference_fraction"], zenith, zenith, case["azimuth"], int(case["day_of_year"])
    )
    assert gain == pytest.approx(case["gain"], abs=0.012)


@pytest.mark.parametrize("case", REFERENCE_CHAIN_V2)
def test_v2_gain_matches_the_pvlib_chain_with_true_and_apparent_zenith(
    case: dict[str, float],
) -> None:
    inputs = (case["reference_fraction"], case["azimuth"], int(case["day_of_year"]))
    gain = tracking_gain(
        inputs[0], case["true_zenith"], case["apparent_zenith"], inputs[1], inputs[2]
    )
    assert gain == pytest.approx(case["gain"], abs=0.012)


def test_one_refracted_zenith_misses_the_v2_chain_where_refraction_is_largest() -> None:
    low_sun = [case for case in REFERENCE_CHAIN_V2 if case["true_zenith"] > 80]
    misses = [
        abs(
            tracking_gain(
                case["reference_fraction"],
                case["apparent_zenith"],
                case["apparent_zenith"],
                case["azimuth"],
                int(case["day_of_year"]),
            )
            - case["gain"]
        )
        for case in low_sun
    ]
    assert len(low_sun) >= 4
    assert max(misses) > 0.05


def test_v1_gain_equals_main() -> None:
    rows = GOLDEN_V1["tracking_gain"]
    assert len(rows) == 400
    assert [
        row
        for row in rows
        if not _matches_golden(tracking_gain(row[0], row[1], row[1], row[2], row[3]), row[4])
    ] == []


def test_v1_interval_gain_equals_main() -> None:
    rows = GOLDEN_V1["interval_gain"]
    assert len(rows) == 192
    assert [
        row
        for row in rows
        if not _matches_golden(
            interval_gain(
                datetime.fromisoformat(row[0]),
                datetime.fromisoformat(row[1]),
                row[2],
                row[3],
                row[4],
                model=MODEL_V1,
            ),
            row[5],
        )
    ] == []


def _golden_configuration(name: str) -> PlantConfiguration:
    mounting = {"fixed": "fixed", "unknown": "unknown"}.get(name, "tracking")
    binding = SiteBinding(
        capacity_kw=1000.0,
        dc_capacity_kw=None if name == "tracking_without_dc" else 1300.0,
        losses_percent=10.0 if name == "tracking_lossy" else 0.0,
        latitude=LATITUDE,
        longitude=LONGITUDE,
        timezone="Europe/Istanbul",
    )
    return PlantConfiguration(binding=binding, dc_capacity_mw=130.0, mounting_type=mounting)


def _golden_power(row: list[Any], model: str) -> float:
    return reference_power_mw(
        _golden_configuration(row[0]),
        datetime.fromisoformat(row[1]),
        datetime.fromisoformat(row[2]),
        row[3],
        model_version=model,
    )


def _power_matches(row: list[Any], model: str) -> bool:
    return _matches_golden(_golden_power(row, model), row[4])


def test_v1_reference_power_equals_main() -> None:
    rows = GOLDEN_V1["reference_power_mw"]
    assert {row[0] for row in rows} == {
        "tracking",
        "tracking_lossy",
        "tracking_without_dc",
        "fixed",
        "unknown",
    }
    assert [row for row in rows if not _power_matches(row, MODEL_V1)] == []


@pytest.mark.parametrize("model", [MODEL_VERSION, MODEL_V1, MODEL_V2])
def test_configurations_without_tracker_geometry_keep_main_output_under_every_model(
    model: str,
) -> None:
    rows = [
        row
        for row in GOLDEN_V1["reference_power_mw"]
        if row[0] in {"tracking_without_dc", "fixed", "unknown"}
    ]
    assert len(rows) == 15
    assert [row for row in rows if not _power_matches(row, model)] == []


def test_tracker_geometry_versions_differ_only_for_modeled_tracking() -> None:
    rows = GOLDEN_V1["reference_power_mw"]
    changed = {row[0] for row in rows if not _power_matches(row, MODEL_V2)}
    assert changed == {"tracking", "tracking_lossy"}
    assert [
        row for row in rows if row[0].startswith("tracking") and _power_matches(row, MODEL_VERSION)
    ] == [row for row in rows if row[0] == "tracking_without_dc"]


def _observer() -> Observer:
    return Observer(LATITUDE, LONGITUDE, 0.0)


@pytest.mark.parametrize("minute", [0, 15, 30, 150, 600, 990, 1020, 1050])
def test_each_version_takes_its_zeniths_at_the_interval_midpoint(minute: int) -> None:
    start = datetime(2026, 6, 21, 2, 0, tzinfo=UTC) + timedelta(minutes=minute)
    end = start + timedelta(minutes=30)
    midpoint = start + timedelta(minutes=15)
    apparent = sun.zenith(_observer(), midpoint)
    true = sun.zenith(_observer(), midpoint, with_refraction=False)
    azimuth = sun.azimuth(_observer(), midpoint)
    assert true > apparent
    day = midpoint.timetuple().tm_yday
    v1 = interval_gain(start, end, 0.3, LATITUDE, LONGITUDE, model=MODEL_V1)
    v2 = interval_gain(start, end, 0.3, LATITUDE, LONGITUDE, model=MODEL_V2)
    assert v1 == tracking_gain(0.3, apparent, apparent, azimuth, day)
    assert v2 == tracking_gain(0.3, true, apparent, azimuth, day)


def test_v2_keeps_low_sun_and_night_at_unit_gain_by_the_true_zenith() -> None:
    assert tracking_gain(0.2, 87.1, 86.8, 100.0, 172) == 1.0
    assert tracking_gain(0.2, 86.8, 86.8, 100.0, 172) != 1.0
    assert tracking_gain(0.2, 95.0, 94.99, 100.0, 172) == 1.0
    night = datetime(2026, 6, 21, 21, 0, tzinfo=UTC)
    for model in (MODEL_V1, MODEL_V2):
        for reference in (0.0, 0.3):
            gain = interval_gain(
                night, night + timedelta(minutes=30), reference, LATITUDE, LONGITUDE, model=model
            )
            assert gain == 1.0


def test_at_dawn_only_the_refracted_zenith_lifts_the_sun_above_the_cutoff() -> None:
    dawn = []
    for second in range(0, 1800, 20):
        start = datetime(2026, 6, 21, 2, 20, tzinfo=UTC) + timedelta(seconds=second)
        midpoint = start + timedelta(minutes=15)
        true = sun.zenith(_observer(), midpoint, with_refraction=False)
        if true >= tracking.MAX_ZENITH_DEGREES > sun.zenith(_observer(), midpoint):
            dawn.append(start)
    assert dawn
    for start in dawn:
        end = start + timedelta(minutes=30)
        assert interval_gain(start, end, 0.05, LATITUDE, LONGITUDE, model=MODEL_V2) == 1.0
        assert interval_gain(start, end, 0.05, LATITUDE, LONGITUDE, model=MODEL_V1) != 1.0


def test_an_unknown_geometry_version_is_refused() -> None:
    start = datetime(2026, 6, 21, 5, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="tracker geometry"):
        interval_gain(start, start + timedelta(minutes=30), 0.3, 37.8, 34.5, model=MODEL_VERSION)


def test_tracker_is_flat_at_solar_noon_and_never_adjusts_night_or_low_sun() -> None:
    assert tracking_gain(0.7678, 14.36, 14.36, 180.0, 172) == pytest.approx(1.0, abs=1e-3)
    assert tracking_gain(0.0, 40.0, 40.0, 120.0, 172) == 1.0
    assert tracking_gain(0.2, 87.0, 87.0, 100.0, 172) == 1.0
    assert tracking_gain(0.0005, 60.0, 60.0, 120.0, 172) == 1.0


def test_gain_grows_with_clearness_and_loses_diffuse_light_under_overcast() -> None:
    overcast = tracking_gain(0.04, 60.0, 60.0, 110.0, 80)
    clear = tracking_gain(0.42, 60.0, 60.0, 110.0, 80)
    very_clear = tracking_gain(0.6, 60.0, 60.0, 110.0, 80)
    assert 0.75 < overcast < 1.0
    assert clear > 1.5
    assert very_clear > 1.2


def test_backtracking_limits_the_low_sun_gain() -> None:
    low = tracking_gain(0.05, 84.0, 84.0, 95.0, 172)
    assert 0.5 < low < 3.0


def test_interval_gain_uses_the_interval_midpoint_solar_position() -> None:
    start = datetime(2026, 6, 21, 5, 0, tzinfo=UTC)
    gain = interval_gain(
        start, start + timedelta(minutes=30), 0.4738, LATITUDE, LONGITUDE, model=MODEL_V1
    )
    assert gain == pytest.approx(1.7547, abs=0.012)
    mirrored = interval_gain(
        start + timedelta(hours=9),
        start + timedelta(hours=9, minutes=30),
        0.4738,
        LATITUDE,
        LONGITUDE,
        model=MODEL_V1,
    )
    assert mirrored == pytest.approx(gain, abs=0.08)


def _configuration(**binding: object) -> PlantConfiguration:
    values: dict[str, object] = {
        "capacity_kw": 1000.0,
        "dc_capacity_kw": 1000.0,
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
        {"dc_capacity_kw": None},
        {"dc_capacity_kw": 0.0},
        {"losses_percent": 100.0},
    ],
)
def test_geometry_requires_known_dc_capacity_and_bounded_losses(
    binding: dict[str, object],
) -> None:
    configuration = _configuration(**binding)
    start = datetime(2026, 6, 21, 5, 0, tzinfo=UTC)
    assert not tracking_modeled(configuration)
    assert transfer_model(configuration, None) == MODEL_VERSION
    for model in (MODEL_V1, MODEL_V2):
        assert reference_power_mw(
            configuration, start, start + timedelta(minutes=30), 473.8, model_version=model
        ) == pytest.approx(0.4738 * 130)


def test_non_tracking_configurations_keep_the_direct_reference_transfer() -> None:
    configuration = _configuration()
    configuration.mounting_type = "fixed"
    start = datetime(2026, 6, 21, 5, 0, tzinfo=UTC)
    assert transfer_model(configuration, None) == MODEL_VERSION
    assert reference_power_mw(
        configuration, start, start + timedelta(minutes=30), 473.8, model_version=MODEL_VERSION
    ) == pytest.approx(0.4738 * 130)


@pytest.mark.parametrize("mounting", ["tracking", "fixed"])
def test_an_unknown_recorded_transfer_model_is_refused(mounting: str) -> None:
    configuration = _configuration()
    configuration.mounting_type = mounting
    start = datetime(2026, 6, 21, 5, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="transfer model"):
        reference_power_mw(
            configuration,
            start,
            start + timedelta(minutes=30),
            473.8,
            model_version="tracking-geometry-transfer-v3",
        )


@pytest.mark.parametrize("default", [MODEL_V1, MODEL_V2])
def test_tracking_transfer_applies_the_default_gain_to_loss_free_reference_power(
    monkeypatch: pytest.MonkeyPatch, default: str
) -> None:
    monkeypatch.setattr(tracking, "DEFAULT_MODEL", default)
    start = datetime(2026, 6, 21, 5, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    configuration = _configuration()
    assert tracking_modeled(configuration)
    model = transfer_model(configuration, None)
    assert model == default
    power = reference_power_mw(configuration, start, end, 473.8, model_version=model)
    assert power == pytest.approx(0.4738 * 130 * 1.7547, rel=0.01)
    assert power == 0.4738 * 130 * interval_gain(
        start, end, 0.4738, LATITUDE, LONGITUDE, model=default
    )
    lossy = _configuration(losses_percent=10.0)
    assert reference_power_mw(lossy, start, end, 473.8 * 0.9, model_version=model) == pytest.approx(
        power * 0.9
    )
    assert reference_power_mw(
        configuration, start, end, 473.8, model_version=MODEL_VERSION
    ) == pytest.approx(0.4738 * 130)


@pytest.mark.parametrize("default", [MODEL_V1, MODEL_V2])
def test_calibrations_apply_under_the_transfer_family_that_fitted_them(
    monkeypatch: pytest.MonkeyPatch, default: str
) -> None:
    monkeypatch.setattr(tracking, "DEFAULT_MODEL", default)
    tracker, fixed = _configuration(), _configuration()
    fixed.mounting_type = "fixed"
    legacy = CalibrationRun(result={})
    v1 = CalibrationRun(result={"transfer_model": MODEL_V1})
    v2 = CalibrationRun(result={"transfer_model": MODEL_V2})
    assert calibration_matches_transfer(legacy, fixed)
    assert not calibration_matches_transfer(legacy, tracker)
    for run in (v1, v2):
        assert calibration_matches_transfer(run, tracker)
        assert not calibration_matches_transfer(run, fixed)
    assert not calibration_matches_transfer(CalibrationRun(result={"transfer_model": 1}), fixed)
    assert not calibration_matches_transfer(
        CalibrationRun(result={"transfer_model": "tracking-geometry-transfer-v3"}), tracker
    )


@pytest.mark.parametrize("default", [MODEL_V1, MODEL_V2])
def test_a_tracker_plant_keeps_the_version_of_its_calibration_and_takes_the_default_without_one(
    monkeypatch: pytest.MonkeyPatch, default: str
) -> None:
    monkeypatch.setattr(tracking, "DEFAULT_MODEL", default)
    tracker, fixed = _configuration(), _configuration()
    fixed.mounting_type = "fixed"
    for recorded in (MODEL_V1, MODEL_V2):
        run = CalibrationRun(result={"transfer_model": recorded})
        assert transfer_model(tracker, run) == recorded
        assert transfer_model(fixed, run) == MODEL_VERSION
    assert transfer_model(tracker, None) == default
    assert transfer_model(tracker, CalibrationRun(result={})) == default
    assert transfer_model(fixed, CalibrationRun(result={})) == MODEL_VERSION
    assert tracking.DEFAULT_MODEL in tracking.MODELS
