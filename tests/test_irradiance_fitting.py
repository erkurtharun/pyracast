from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import fsum

import pytest

from pyracast.irradiance.fitting import fit_quadratic
from pyracast.irradiance.pairs import PairedInterval, WeatherInterval

START = datetime(2026, 1, 1, tzinfo=UTC)
COEFFICIENTS = (-0.026, 1.036, -0.001, -0.103, -0.174, 0.002)


def sample(ghi: float, temperature: float, *, varying: bool = True) -> PairedInterval:
    rows: list[WeatherInterval] = []
    powers: list[float] = []
    for index in range(6):
        delta = index - 2.5 if varying else 0
        native_g = ghi + delta * 8
        native_t = temperature + delta
        loss = index * 0.1 if varying else 0
        start = START + timedelta(minutes=index * 5)
        rows.append(WeatherInterval(start, start + timedelta(minutes=5), native_g, native_t, loss))
        g, t = native_g / 1000, (native_t - 25) / 25
        value = -0.026 + 1.036 * g - 0.001 * t - 0.103 * g * t - 0.174 * g * g + 0.002 * t * t
        powers.append(1000 * (1 - loss) * value)
    return PairedInterval(
        START,
        START + timedelta(minutes=30),
        fsum(powers) / 6,
        ghi,
        temperature,
        0.25 if varying else 0,
        tuple(rows),
    )


def training(*, varying: bool = True) -> list[PairedInterval]:
    return [
        sample(ghi, temperature, varying=varying)
        for ghi in (100.0, 300.0, 600.0, 900.0)
        for temperature in (-5.0, 10.0, 25.0, 40.0)
    ]


def test_recovers_independent_polynomial_and_native_moments() -> None:
    result = fit_quadratic(training(), capacity_kw=1000)
    assert result.coefficients == pytest.approx(COEFFICIENTS, abs=1e-12)
    assert result.sample_count == 16
    assert result.min_temperature_c == -7.5
    assert result.max_temperature_c == 42.5
    constant = fit_quadratic(training(varying=False), capacity_kw=1000)
    assert constant.coefficients == pytest.approx(COEFFICIENTS, abs=1e-12)


def test_aggregate_features_do_not_replace_native_products() -> None:
    rows = [replace(row, ghi_wm2=0, air_temp_c=100, snow_loss_fraction=1) for row in training()]
    assert fit_quadratic(rows, capacity_kw=1000).coefficients == pytest.approx(
        COEFFICIENTS, abs=1e-12
    )


def test_zero_low_light_and_high_snow_do_not_enter_training() -> None:
    rows = training()
    rows.extend(
        [
            replace(rows[0], power_kw=0),
            sample(40, 10),
            replace(
                rows[0],
                weather=(
                    replace(rows[0].weather[0], snow_loss_fraction=0.96),
                    *rows[0].weather[1:],
                ),
            ),
        ]
    )
    fitted = fit_quadratic(rows, capacity_kw=1000)
    assert fitted.sample_count == 16
    assert fitted.coefficients == pytest.approx(COEFFICIENTS, abs=1e-12)


def test_capacity_is_normalization_without_an_assumed_clipping_limit() -> None:
    result = fit_quadratic(training(), capacity_kw=100)
    assert result.sample_count == 16
    assert result.coefficients == pytest.approx(tuple(value * 10 for value in COEFFICIENTS))


@pytest.mark.parametrize("capacity", [0.0, -1.0, float("nan"), float("inf")])
def test_rejects_invalid_capacity(capacity: float) -> None:
    with pytest.raises(ValueError, match="Capacity"):
        fit_quadratic(training(), capacity_kw=capacity)


def test_requires_sufficient_eligible_pairs() -> None:
    with pytest.raises(ValueError, match="six eligible"):
        fit_quadratic(training()[:5], capacity_kw=1000)


@pytest.mark.parametrize("same_temperature", [False, True])
def test_rejects_rank_deficient_training(same_temperature: bool) -> None:
    rows = (
        [sample(100 + index * 50, 25, varying=False) for index in range(10)]
        if same_temperature
        else [sample(500, 10)] * 10
    )
    with pytest.raises(ValueError, match="rank deficient"):
        fit_quadratic(rows, capacity_kw=1000)


@pytest.mark.parametrize("power", [-1.0, float("nan"), float("inf")])
def test_rejects_invalid_power(power: float) -> None:
    with pytest.raises(ValueError, match="Power"):
        fit_quadratic([replace(training()[0], power_kw=power)], capacity_kw=1000)


@pytest.mark.parametrize("kind", ["missing", "duration", "gap", "native_duration"])
def test_rejects_incomplete_native_coverage(kind: str) -> None:
    row = training()[0]
    if kind == "missing":
        row = replace(row, weather=row.weather[:5])
    elif kind == "duration":
        row = replace(row, end=row.end + timedelta(seconds=1))
    elif kind == "gap":
        row = replace(
            row,
            weather=(
                replace(row.weather[0], start=row.start + timedelta(seconds=1)),
                *row.weather[1:],
            ),
        )
    else:
        row = replace(
            row,
            weather=(
                replace(row.weather[0], end=row.weather[0].end + timedelta(seconds=1)),
                *row.weather[1:],
            ),
        )
    with pytest.raises(ValueError, match=r"pair|cover"):
        fit_quadratic([row], capacity_kw=1000)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ghi_wm2", -1.0),
        ("ghi_wm2", float("inf")),
        ("air_temp_c", float("nan")),
        ("snow_loss_fraction", -0.1),
        ("snow_loss_fraction", 1.1),
        ("ghi_wm2", 1e300),
    ],
)
def test_rejects_invalid_native_values(field: str, value: float) -> None:
    row = training()[0]
    original = row.weather[0]
    native = WeatherInterval(
        original.start,
        original.end,
        value if field == "ghi_wm2" else original.ghi_wm2,
        value if field == "air_temp_c" else original.air_temp_c,
        value if field == "snow_loss_fraction" else original.snow_loss_fraction,
    )
    row = replace(row, weather=(native, *row.weather[1:]))
    with pytest.raises(ValueError, match="weather"):
        fit_quadratic([row], capacity_kw=1000)


def test_rejects_normalized_power_overflow() -> None:
    with pytest.raises(ValueError, match="Normalized power"):
        fit_quadratic(training(), capacity_kw=1e-320)


def test_rejects_unrepresentable_coefficients() -> None:
    rows = [
        replace(
            sample(ghi, temperature, varying=False), power_kw=1e308 * (1 + (temperature - 25) * 9)
        )
        for ghi in (100.0, 300.0, 600.0, 900.0)
        for temperature in (25.0, 25.025, 25.05, 25.075)
    ]
    with pytest.raises(ValueError, match="coefficients overflow"):
        fit_quadratic(rows, capacity_kw=1)
