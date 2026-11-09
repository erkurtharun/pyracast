import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from pyracast.plants.numerics import CalibrationSample, PowerInterval, fit_calibration, forecast

START = datetime(2026, 6, 1, tzinfo=UTC)


def _samples(days: int, *, factor: float = 0.8, power: float = 40) -> tuple[CalibrationSample, ...]:
    result = []
    for day in range(days):
        for hour in range(24):
            start = START + timedelta(days=day, hours=hour)
            end = start + timedelta(hours=1)
            native_power = power if 6 <= hour < 18 else 0.0
            result.append(
                CalibrationSample(
                    start, end, native_power * factor, (PowerInterval(start, end, native_power),)
                )
            )
    return tuple(result)


def _fit(samples: tuple[CalibrationSample, ...], cap: float | None = 100) -> object:
    return fit_calibration(samples, timezone="UTC", normalization_mw=100, limit_mw=cap)


def test_native_clipping_precedes_hourly_aggregation() -> None:
    end = START + timedelta(hours=1)
    middle = START + timedelta(minutes=30)
    result = forecast(
        (PowerInterval(START, middle, 80), PowerInterval(middle, end, 120)),
        start=START,
        end=end,
        resolution_minutes=60,
        limit_mw=100,
    )
    assert result["energy_mwh"] == 90
    assert result["complete"] is True
    assert result["coverage_fraction"] == 1
    records = cast(list[dict[str, object]], result["intervals"])
    assert records[0]["mean_power_mw"] == 90
    assert json.loads(json.dumps(result))["model_version"] == "reference-power-transfer-v1"


def test_missing_energy_is_partial_and_not_zero() -> None:
    middle = START + timedelta(minutes=30)
    end = START + timedelta(hours=1)
    result = forecast(
        (PowerInterval(START, middle, 80), PowerInterval(middle, end, None)),
        start=START,
        end=end,
        resolution_minutes=60,
        limit_mw=None,
        factor=0.5,
    )
    assert result["energy_mwh"] is None
    assert result["partial_energy_mwh"] == 20
    assert result["coverage_fraction"] == 0.5
    assert result["quality_flags"] == ["unvalidated_plant_proxy", "incomplete"]
    records = cast(list[dict[str, object]], result["intervals"])
    assert records[0]["mean_power_mw"] is None
    assert records[0]["energy_mwh"] is None


def test_empty_partial_overlap_and_final_short_interval() -> None:
    end = START + timedelta(minutes=45)
    result = forecast((), start=START, end=end, resolution_minutes=30, limit_mw=100)
    assert result["partial_energy_mwh"] == 0
    assert result["coverage_fraction"] == 0
    result = forecast(
        (PowerInterval(START - timedelta(hours=1), end + timedelta(hours=1), 40),),
        start=START,
        end=end,
        resolution_minutes=30,
        limit_mw=None,
    )
    assert result["energy_mwh"] == 30
    assert len(cast(list[object], result["intervals"])) == 2
    assert result["quality_flags"] == ["unvalidated_plant_proxy", "uniform_mean_proration"]


def test_nonfinite_computed_energy_is_rejected() -> None:
    end = START + timedelta(hours=1)
    with pytest.raises(ValueError, match="Calculated energy must be finite"):
        forecast(
            (PowerInterval(START, end, 1e308),),
            start=START,
            end=end,
            resolution_minutes=60,
            limit_mw=None,
            factor=2,
        )


def test_nonfinite_daily_energy_accumulation_is_rejected() -> None:
    end = START + timedelta(days=1)
    with pytest.raises(ValueError, match="Forecast total energy must be finite"):
        forecast(
            (PowerInterval(START, end, 1e308),),
            start=START,
            end=end,
            resolution_minutes=30,
            limit_mw=None,
        )


def test_nonfinite_calibration_metrics_are_rejected() -> None:
    samples = list(_samples(7))
    for index in range(5 * 24, len(samples)):
        samples[index] = replace(samples[index], energy_mwh=1e308)
    with pytest.raises(ValueError, match="Calibration metric must be finite"):
        fit_calibration(tuple(samples), timezone="UTC", normalization_mw=130, limit_mw=100)


def test_nonfinite_calibration_loss_is_rejected() -> None:
    samples = tuple(replace(sample, energy_mwh=2.0) for sample in _samples(7, power=5e-324))
    with pytest.raises(ValueError, match="Calibration loss must be finite"):
        fit_calibration(samples, timezone="UTC", normalization_mw=1e-308, limit_mw=None)


def test_nonfinite_calibration_gradient_is_rejected() -> None:
    with pytest.raises(ValueError, match="Calibration gradient must be finite"):
        fit_calibration(_samples(7), timezone="UTC", normalization_mw=1e-308, limit_mw=None)


@pytest.mark.parametrize(("month", "day", "hours"), [(3, 29, 23), (10, 25, 25)])
def test_dst_energy_uses_explicit_utc_duration(month: int, day: int, hours: int) -> None:
    zone = ZoneInfo("Europe/Berlin")
    start = datetime(2026, month, day, tzinfo=zone)
    end = start + timedelta(days=1)
    result = forecast(
        (PowerInterval(start, end, 10),), start=start, end=end, resolution_minutes=60, limit_mw=100
    )
    assert result["energy_mwh"] == hours * 10
    assert len(cast(list[object], result["intervals"])) == hours


@pytest.mark.parametrize("power", [float("nan"), float("inf"), -1.0])
def test_invalid_native_power_rejected(power: float) -> None:
    end = START + timedelta(hours=1)
    with pytest.raises(ValueError, match="Power must"):
        forecast(
            (PowerInterval(START, end, power),),
            start=START,
            end=end,
            resolution_minutes=30,
            limit_mw=100,
        )


@pytest.mark.parametrize(
    ("factor", "cap"), [(0, 100), (-1, 100), (float("nan"), 100), (1, 0), (1, float("inf"))]
)
def test_invalid_scale_and_cap_rejected(factor: float, cap: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        forecast(
            (),
            start=START,
            end=START + timedelta(hours=1),
            resolution_minutes=30,
            limit_mw=cap,
            factor=factor,
        )


def test_invalid_bounds_resolution_and_overlap_rejected() -> None:
    end = START + timedelta(hours=1)
    with pytest.raises(ValueError, match="timezone-aware"):
        forecast((), start=START.replace(tzinfo=None), end=end, resolution_minutes=30, limit_mw=100)
    with pytest.raises(ValueError, match="timezone-aware"):
        forecast((), start=START, end=end.replace(tzinfo=None), resolution_minutes=30, limit_mw=100)
    with pytest.raises(ValueError, match="end must follow"):
        forecast((), start=START, end=START, resolution_minutes=30, limit_mw=100)
    with pytest.raises(ValueError, match="Resolution"):
        forecast((), start=START, end=end, resolution_minutes=15, limit_mw=100)
    with pytest.raises(ValueError, match="overlap"):
        forecast(
            (PowerInterval(START, end, 1), PowerInterval(START, end, 2)),
            start=START,
            end=end,
            resolution_minutes=30,
            limit_mw=100,
        )


@pytest.mark.parametrize("days", [7, 10, 30])
def test_known_factor_is_shrunk_and_evaluated_on_future_days(days: int) -> None:
    result = fit_calibration(_samples(days), timezone="UTC", normalization_mw=100, limit_mw=100)
    assert result.status == "ready"
    assert result.factor is not None
    assert 0.8 < result.factor < 0.81
    assert result.assessment == ("evaluated" if days == 30 else "provisional")
    assert result.report["valid_days"] == days
    assert result.report["holdout_days"] == (7 if days == 30 else 2)
    included_dates = [f"2026-06-{day:02d}" for day in range(1, days + 1)]
    training_count = {7: 5, 10: 8, 30: 23}[days]
    assert result.report["included_day_dates"] == included_dates
    assert result.report["training_day_dates"] == included_dates[:training_count]
    assert result.report["holdout_day_dates"] == included_dates[training_count:]
    assert result.report["uncertainty"] == "not_estimated"
    baseline = cast(dict[str, float], result.report["baseline"])
    candidate = cast(dict[str, float], result.report["candidate"])
    assert baseline["interval_mae_mw"] == 4
    assert baseline["daily_mae_mwh"] == 96
    assert baseline["daily_bias_mwh"] == 96
    assert candidate["interval_mae_mw"] < 0.1
    assert candidate["daily_bias_mwh"] == pytest.approx(1.185185185185)


def test_outlier_has_bounded_effect_and_identity_is_retained_without_improvement() -> None:
    samples = list(_samples(7))
    samples[6] = replace(samples[6], energy_mwh=97)
    result = fit_calibration(tuple(samples), timezone="UTC", normalization_mw=100, limit_mw=100)
    assert result.status == "ready"
    assert result.factor is not None
    assert 0.8 < result.factor < 0.82
    identity = fit_calibration(
        _samples(7, factor=1), timezone="UTC", normalization_mw=100, limit_mw=None
    )
    assert identity.status == "rejected"
    assert identity.factor is None


def test_future_samples_are_not_refit_and_worse_holdout_rejects_candidate() -> None:
    samples = list(_samples(7))
    for index in range(5 * 24, len(samples)):
        sample = samples[index]
        samples[index] = replace(sample, energy_mwh=sample.energy_mwh / 0.8)
    result = fit_calibration(tuple(samples), timezone="UTC", normalization_mw=100, limit_mw=100)
    assert result.status == "rejected"
    assert cast(float, result.report["candidate_factor"]) < 0.81
    assert result.report["holdout_start"] == "2026-06-06"
    candidate = cast(dict[str, float], result.report["candidate"])
    assert candidate["daily_bias_mwh"] == pytest.approx(-94.814814814815)


@pytest.mark.parametrize("days", [0, 6])
def test_insufficient_days(days: int) -> None:
    result = fit_calibration(_samples(days), timezone="UTC", normalization_mw=100, limit_mw=100)
    assert result.status == "insufficient"
    assert result.assessment == "insufficient"
    assert result.factor is None
    assert result.report["training_day_dates"] == []
    assert result.report["holdout_day_dates"] == []


def test_zero_days_and_censored_only_samples_are_not_informative() -> None:
    result = fit_calibration(
        _samples(7, power=0), timezone="UTC", normalization_mw=100, limit_mw=None
    )
    assert result.status == "insufficient"
    assert result.report["valid_days"] == 0
    clipped = fit_calibration(
        _samples(7, factor=1, power=100), timezone="UTC", normalization_mw=100, limit_mw=100
    )
    assert clipped.status == "insufficient"
    assert clipped.report["fit_samples"] == 0


def test_training_excludes_censoring_but_holdout_retains_clipped_intervals() -> None:
    samples = list(_samples(7))
    for day in range(7):
        index = day * 24 + 12
        sample = samples[index]
        samples[index] = replace(
            sample, energy_mwh=100, intervals=(PowerInterval(sample.start, sample.end, 150),)
        )
    result = fit_calibration(tuple(samples), timezone="UTC", normalization_mw=100, limit_mw=100)
    assert result.status == "ready"
    assert cast(dict[str, int], result.report["exclusions"])["censored_training"] == 5
    assert cast(dict[str, int], result.report["exclusions"])["uninformative_training"] == 60
    assert result.report["samples"] == 168


@pytest.mark.parametrize("kind", ["outage", "curtailed", "invalid", "source", "meter"])
def test_incomplete_or_nonoperational_day_cannot_meet_minimum(kind: str) -> None:
    samples = list(_samples(7))
    sample = samples[7]
    if kind == "source":
        samples[7] = replace(sample, intervals=(PowerInterval(sample.start, sample.end, None),))
    elif kind == "meter":
        samples.pop(7)
    else:
        samples[7] = replace(sample, quality=kind)
    result = fit_calibration(tuple(samples), timezone="UTC", normalization_mw=100, limit_mw=100)
    assert result.status == "insufficient"
    assert result.report["valid_days"] == 6


def test_fit_rejects_invalid_meter_configuration_and_bounds() -> None:
    sample = _samples(1)[0]
    with pytest.raises(ValueError, match="finite and positive"):
        fit_calibration((), timezone="UTC", normalization_mw=0, limit_mw=None)
    with pytest.raises(ValueError, match="finite and positive"):
        fit_calibration((), timezone="UTC", normalization_mw=100, limit_mw=-1)
    with pytest.raises(ValueError, match="Meter energy"):
        _fit((replace(sample, energy_mwh=float("nan")),))
    with pytest.raises(ValueError, match="Meter energy"):
        _fit((replace(sample, energy_mwh=-1),))
    with pytest.raises(ValueError, match="Meter intervals must not overlap"):
        _fit((sample, sample))
    with pytest.raises(ValueError, match="cross local midnight"):
        _fit((replace(sample, end=sample.start + timedelta(days=1, hours=1)),))
    with pytest.raises(ValueError, match="timezone-aware"):
        _fit((sample, replace(sample, start=sample.start.replace(tzinfo=None))))


@pytest.mark.parametrize("factor", [0.1, 3])
def test_bound_optimum_is_rejected(factor: float) -> None:
    result = fit_calibration(
        _samples(7, factor=factor, power=10), timezone="UTC", normalization_mw=10, limit_mw=None
    )
    assert result.status == "rejected"
    assert result.factor is None


def test_fit_optimizes_native_capped_energy_instead_of_capped_average() -> None:
    samples = []
    for day in range(7):
        for hour in range(24):
            start = START + timedelta(days=day, hours=hour)
            middle, end = start + timedelta(minutes=30), start + timedelta(hours=1)
            samples.append(
                CalibrationSample(
                    start,
                    end,
                    90,
                    (PowerInterval(start, middle, 80), PowerInterval(middle, end, 200)),
                )
            )
    result = fit_calibration(tuple(samples), timezone="UTC", normalization_mw=100, limit_mw=100)
    assert result.status == "rejected"
    assert cast(float, result.report["candidate_factor"]) == pytest.approx(1)


def test_mixed_native_piece_slope_has_interior_optimum() -> None:
    samples = []
    for day in range(7):
        for hour in range(24):
            start = START + timedelta(days=day, hours=hour)
            middle, end = start + timedelta(minutes=30), start + timedelta(hours=1)
            samples.append(
                CalibrationSample(
                    start,
                    end,
                    82,
                    (PowerInterval(start, middle, 80), PowerInterval(middle, end, 200)),
                )
            )
    result = fit_calibration(tuple(samples), timezone="UTC", normalization_mw=100, limit_mw=100)
    assert result.status == "ready"
    assert result.factor is not None
    assert 0.8 < result.factor < 0.81
