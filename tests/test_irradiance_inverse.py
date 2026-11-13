from dataclasses import replace
from typing import cast

import pytest

from pyracast.irradiance import SourceKind, Sunlight
from pyracast.irradiance.inverse import (
    Coefficients,
    QuadraticModel,
    forward_power,
    reconstruct_ghi,
)

MODEL = QuadraticModel((-0.025, 1.03, 0, -0.1, -0.17, 0.002), -10, 35)


@pytest.mark.parametrize(
    ("temperature", "snow", "ghi", "capacity"),
    [(25, 0, 600, 1000), (0, 0.4, 500, 1), (35, 0.8, 900, 1000), (25, 0, 1400, 1000)],
)
def test_forward_inverse_round_trip_on_increasing_branch(
    temperature: float, snow: float, ghi: float, capacity: float
) -> None:
    power = forward_power(
        ghi,
        capacity_kw=capacity,
        air_temp_c=temperature,
        snow_loss_fraction=snow,
        model=MODEL,
    )
    estimate = reconstruct_ghi(
        power,
        capacity_kw=capacity,
        air_temp_c=temperature,
        snow_loss_fraction=snow,
        model=MODEL,
    )
    assert estimate.ghi_wm2 == pytest.approx(ghi)
    assert estimate.sensitivity_wm2_per_kw is not None
    assert estimate.sensitivity_wm2_per_kw > 0


def test_independent_quadratic_and_linear_solutions_and_sensitivity() -> None:
    quadratic = QuadraticModel((-0.02, 1, 0, 0, -0.1, 0), 0, 40)
    result = reconstruct_ghi(
        455, capacity_kw=1000, air_temp_c=25, snow_loss_fraction=0, model=quadratic
    )
    assert result.ghi_wm2 == pytest.approx(500)
    assert result.sensitivity_wm2_per_kw == pytest.approx(1 / 0.9)
    linear = replace(quadratic, coefficients=(-0.02, 1, 0, 0, 0, 0))
    result = reconstruct_ghi(
        480, capacity_kw=1000, air_temp_c=25, snow_loss_fraction=0, model=linear
    )
    assert result.ghi_wm2 == pytest.approx(500)
    assert result.sensitivity_wm2_per_kw == 1


@pytest.mark.parametrize(
    ("power", "temperature", "snow", "sunlight", "capped", "flag"),
    [
        (None, 25, 0, "unknown", False, "missing_power"),
        (-1, 25, 0, "day", False, "invalid_power"),
        (float("nan"), 25, 0, "day", False, "invalid_power"),
        (True, 25, 0, "day", False, "invalid_power"),
        (1, 25, 0, "night", False, "night_power_conflict"),
        (0, 25, 0, "day", False, "zero_power_not_identifiable"),
        (1000, 25, 0, "day", True, "capped_power_not_identifiable"),
        (500, None, 0, "day", False, "weather_inputs_required"),
        (500, 25, None, "day", False, "weather_inputs_required"),
        (500, float("nan"), 0, "day", False, "invalid_weather"),
        (500, 25, -0.1, "day", False, "invalid_weather"),
        (500, 25, float("inf"), "day", False, "invalid_weather"),
        (500, True, 0, "day", False, "invalid_weather"),
        (500, 25, True, "day", False, "invalid_weather"),
        (500, 25, 1, "day", False, "snow_opaque"),
        (5, 25, 0.99, "day", False, "snow_ill_conditioned"),
        (1e308, 25, 0, "day", False, "no_supported_root"),
    ],
)
def test_unidentified_or_invalid_inputs_preserve_missingness(
    power: float | None,
    temperature: float | None,
    snow: float | None,
    sunlight: str,
    capped: bool,
    flag: str,
) -> None:
    result = reconstruct_ghi(
        power,
        capacity_kw=1000,
        air_temp_c=temperature,
        snow_loss_fraction=snow,
        model=MODEL,
        sunlight=cast(Sunlight, sunlight),
        power_is_capped=capped,
    )
    assert result.ghi_wm2 is None
    assert result.sensitivity_wm2_per_kw is None
    assert flag in result.quality_flags


def test_night_does_not_require_weather_and_forecast_transfer_remains_unvalidated() -> None:
    result = reconstruct_ghi(
        0,
        capacity_kw=1000,
        air_temp_c=None,
        snow_loss_fraction=None,
        model=MODEL,
        sunlight="night",
        source_kind="forecast",
    )
    assert result.ghi_wm2 == 0
    assert "declared_night" in result.quality_flags
    assert "forecast_transfer_unvalidated" in result.quality_flags


def test_domain_and_temperature_extrapolation_are_explicit() -> None:
    result = reconstruct_ghi(
        500, capacity_kw=1000, air_temp_c=40, snow_loss_fraction=0, model=MODEL
    )
    assert result.ghi_wm2 is not None
    assert "temperature_extrapolation" in result.quality_flags
    result = reconstruct_ghi(
        1200, capacity_kw=1000, air_temp_c=25, snow_loss_fraction=0, model=MODEL
    )
    assert result.ghi_wm2 is None
    assert "irradiance_out_of_domain" in result.quality_flags
    intercept = replace(MODEL, coefficients=(0.2, 1, 0, 0, 0, 0))
    assert (
        "irradiance_out_of_domain"
        in reconstruct_ghi(
            1, capacity_kw=1000, air_temp_c=25, snow_loss_fraction=0, model=intercept
        ).quality_flags
    )


def test_ambiguous_branch_and_overflow_are_rejected() -> None:
    nonmonotone = replace(MODEL, coefficients=(0, 1, 0, 0, -1, 0))
    assert (
        "nonmonotone_model_domain"
        in reconstruct_ghi(
            100, capacity_kw=1000, air_temp_c=25, snow_loss_fraction=0, model=nonmonotone
        ).quality_flags
    )
    assert (
        "numerical_failure"
        in reconstruct_ghi(
            100, capacity_kw=1000, air_temp_c=1e308, snow_loss_fraction=0, model=MODEL
        ).quality_flags
    )
    tiny_capacity = replace(MODEL, coefficients=(0, 1, 0, 0, 0, 0))
    assert (
        "numerical_failure"
        in reconstruct_ghi(
            1e-308, capacity_kw=1e-308, air_temp_c=25, snow_loss_fraction=0, model=tiny_capacity
        ).quality_flags
    )


@pytest.mark.parametrize("capacity", [True, 0, -1, float("inf")])
def test_invalid_capacity_is_a_configuration_error(capacity: float) -> None:
    with pytest.raises(ValueError, match="AC reference capacity"):
        reconstruct_ghi(100, capacity_kw=capacity, air_temp_c=25, snow_loss_fraction=0, model=MODEL)


def test_invalid_source_kind_and_sunlight_are_rejected() -> None:
    with pytest.raises(ValueError, match="source kind"):
        reconstruct_ghi(
            100,
            capacity_kw=1000,
            air_temp_c=25,
            snow_loss_fraction=0,
            model=MODEL,
            source_kind=cast(SourceKind, "measured"),
        )
    with pytest.raises(ValueError, match="sunlight"):
        reconstruct_ghi(
            100,
            capacity_kw=1000,
            air_temp_c=25,
            snow_loss_fraction=0,
            model=MODEL,
            sunlight=cast(Sunlight, "sunny"),
        )


@pytest.mark.parametrize(
    "values",
    [
        {"coefficients": cast(Coefficients, (1,))},
        {"coefficients": (0, float("nan"), 0, 0, 0, 0)},
        {"min_temperature_c": float("nan")},
        {"max_temperature_c": float("inf")},
        {"min_temperature_c": 50},
        {"max_ghi_wm2": 0},
        {"max_ghi_wm2": float("inf")},
        {"min_transmission": 0},
        {"min_transmission": float("nan")},
        {"version": ""},
    ],
)
def test_invalid_model_parameters_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"required|range|limit|transmission"):
        replace(MODEL, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("ghi", "capacity", "temperature", "snow"),
    [(-1, 1000, 25, 0), (1, 0, 25, 0), (1, 1000, float("nan"), 0), (1, 1000, 25, 2)],
)
def test_forward_model_rejects_invalid_inputs(
    ghi: float, capacity: float, temperature: float, snow: float
) -> None:
    with pytest.raises(ValueError, match="Finite irradiance"):
        forward_power(
            ghi,
            capacity_kw=capacity,
            air_temp_c=temperature,
            snow_loss_fraction=snow,
            model=MODEL,
        )
