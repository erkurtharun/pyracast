import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from pyracast.plants.numerics import (
    CalibrationSample,
    PowerInterval,
    evaluate_samples,
    fit_calibration,
    forecast,
)

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


def test_daylight_only_samples_fit_without_fabricating_night_energy() -> None:
    samples = tuple(sample for sample in _samples(7) if sample.start.hour in range(6, 18))
    windows = {
        (START + timedelta(days=offset)).date(): (
            (
                START + timedelta(days=offset, hours=6, minutes=15),
                START + timedelta(days=offset, hours=17, minutes=45),
            ),
        )
        for offset in range(7)
    }
    result = fit_calibration(
        samples,
        timezone="UTC",
        normalization_mw=100,
        limit_mw=100,
        required_windows=windows,
    )
    assert result.status == "ready"
    assert result.report["valid_days"] == 7
    assert result.report["samples"] == 84
    assert result.report["metric_scope"] == "daylight_intervals"
    evaluated = evaluate_samples(
        samples,
        timezone="UTC",
        limit_mw=100,
        required_windows=windows,
    )
    daily = cast(list[dict[str, object]], evaluated["day_errors"])
    assert daily[0]["measured_energy_mwh"] == 384
    assert daily[0]["predicted_energy_mwh"] == 480


@pytest.mark.parametrize("hour", [6, 12, 17])
@pytest.mark.parametrize("quality", ["missing", "invalid", "outage", "curtailed", "source_gap"])
def test_daylight_boundary_or_internal_gap_invalidates_day(hour: int, quality: str) -> None:
    samples = tuple(sample for sample in _samples(1) if sample.start.hour in range(6, 18))
    selected = samples[hour - 6]
    if quality == "missing":
        samples = tuple(sample for sample in samples if sample != selected)
    else:
        replacement = (
            replace(selected, intervals=())
            if quality == "source_gap"
            else replace(selected, quality=quality)
        )
        samples = tuple(replacement if sample == selected else sample for sample in samples)
    result = evaluate_samples(
        samples,
        timezone="UTC",
        limit_mw=100,
        required_windows={
            START.date(): (
                (START + timedelta(hours=6, minutes=15), START + timedelta(hours=17, minutes=45)),
            )
        },
    )
    assert result["valid_days"] == 0
    assert result["metrics"] is None


def test_polar_night_never_becomes_a_complete_day() -> None:
    result = evaluate_samples(
        _samples(1),
        timezone="UTC",
        limit_mw=100,
        required_windows={START.date(): ()},
    )
    assert result["valid_days"] == 0
    assert result["samples"] == 0
    assert (
        evaluate_samples(
            (),
            timezone="UTC",
            limit_mw=100,
            required_windows={START.date(): ()},
        )["valid_days"]
        == 0
    )


def test_optional_night_quality_does_not_affect_daylight_but_input_is_validated() -> None:
    samples = tuple(
        replace(sample, quality="outage") if sample.start.hour < 6 else sample
        for sample in _samples(1)
    )
    windows = {START.date(): ((START + timedelta(hours=6), START + timedelta(hours=18)),)}
    result = evaluate_samples(samples, timezone="UTC", limit_mw=100, required_windows=windows)
    exclusions = cast(dict[str, int], result["exclusions"])
    assert result["valid_days"] == 1
    assert exclusions["outside_required_windows"] == 12
    assert exclusions["quality"] == 0
    with pytest.raises(ValueError, match="nonnegative"):
        evaluate_samples(
            (replace(samples[0], energy_mwh=-1), *samples[1:]),
            timezone="UTC",
            limit_mw=100,
            required_windows=windows,
        )
    with pytest.raises(ValueError, match="overlap"):
        evaluate_samples(
            (*samples, samples[0]),
            timezone="UTC",
            limit_mw=100,
            required_windows=windows,
        )


def test_multiple_daylight_windows_preserve_the_union_and_full_meter_energy() -> None:
    windows = {
        START.date(): (
            (START + timedelta(hours=1, minutes=15), START + timedelta(hours=2, minutes=45)),
            (START + timedelta(hours=21, minutes=15), START + timedelta(hours=22, minutes=45)),
        )
    }
    samples = tuple(replace(sample, energy_mwh=1) for sample in _samples(1))
    result = evaluate_samples(samples, timezone="UTC", limit_mw=100, required_windows=windows)
    assert result["valid_days"] == 1
    assert result["samples"] == 4
    daily = cast(list[dict[str, object]], result["day_errors"])
    assert daily[0]["measured_energy_mwh"] == 4
    missing_last = tuple(sample for sample in samples if sample.start.hour != 22)
    assert (
        evaluate_samples(
            missing_last,
            timezone="UTC",
            limit_mw=100,
            required_windows=windows,
        )["valid_days"]
        == 0
    )
    missing_first_window = tuple(sample for sample in samples if sample.start.hour not in (1, 2))
    assert (
        evaluate_samples(
            missing_first_window,
            timezone="UTC",
            limit_mw=100,
            required_windows=windows,
        )["valid_days"]
        == 0
    )


@pytest.mark.parametrize(
    "windows",
    [
        ((START - timedelta(seconds=1), START + timedelta(hours=1)),),
        ((START, START + timedelta(days=1, seconds=1)),),
        (
            (START, START + timedelta(hours=2)),
            (START + timedelta(hours=1), START + timedelta(hours=3)),
        ),
    ],
)
def test_invalid_required_windows_are_rejected(
    windows: tuple[tuple[datetime, datetime], ...],
) -> None:
    with pytest.raises(ValueError, match="Required windows"):
        evaluate_samples(
            (),
            timezone="UTC",
            limit_mw=100,
            required_windows={START.date(): windows},
        )


def test_selected_boundary_meter_requires_source_even_outside_daylight() -> None:
    sample = CalibrationSample(
        START + timedelta(hours=6),
        START + timedelta(hours=7),
        10,
        (PowerInterval(START + timedelta(hours=6, minutes=15), START + timedelta(hours=7), 20),),
    )
    result = evaluate_samples(
        (sample,),
        timezone="UTC",
        limit_mw=100,
        required_windows={START.date(): ((sample.intervals[0].start, sample.end),)},
    )
    assert result["valid_days"] == 0
    assert cast(dict[str, int], result["exclusions"])["source_incomplete"] == 1


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
    samples = tuple(replace(sample, energy_mwh=2.0) for sample in _samples(7, power=1e-308))
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
    evaluated = evaluate_samples(
        (CalibrationSample(start, end, hours * 8, (PowerInterval(start, end, 10),)),),
        timezone=str(zone),
        limit_mw=100,
        required_windows={start.date(): ((start.astimezone(UTC), end.astimezone(UTC)),)},
    )
    assert evaluated["valid_days"] == 1
    daily = cast(list[dict[str, object]], evaluated["day_errors"])
    assert daily[0]["measured_energy_mwh"] == hours * 8
    assert daily[0]["predicted_energy_mwh"] == hours * 10


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


@pytest.mark.parametrize("daylight_only", [False, True])
def test_fit_optimizes_native_capped_energy_instead_of_capped_average(daylight_only: bool) -> None:
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
    windows = (
        {
            (START + timedelta(days=day)).date(): (
                (
                    START + timedelta(days=day, hours=6, minutes=15),
                    START + timedelta(days=day, hours=17, minutes=45),
                ),
            )
            for day in range(7)
        }
        if daylight_only
        else None
    )
    result = fit_calibration(
        tuple(samples),
        timezone="UTC",
        normalization_mw=100,
        limit_mw=100,
        required_windows=windows,
    )
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


def test_one_informative_hour_per_day_does_not_support_a_fit() -> None:
    samples = tuple(
        sample
        if sample.start.hour == 6
        else replace(sample, energy_mwh=0, intervals=(PowerInterval(sample.start, sample.end, 0),))
        for sample in _samples(7)
    )
    result = fit_calibration(samples, timezone="UTC", normalization_mw=100, limit_mw=100)
    assert result.status == "insufficient"
    assert result.report["reasons"] == ["insufficient_training_support"]


def test_candidate_cannot_replace_a_better_incumbent() -> None:
    result = fit_calibration(
        _samples(7), timezone="UTC", normalization_mw=100, limit_mw=100, incumbent_factor=0.8
    )
    assert result.status == "rejected"
    assert "incumbent_mae_regression" in cast(list[str], result.report["reasons"])


def test_smaller_mae_cannot_hide_worse_absolute_daily_bias() -> None:
    samples = list(_samples(7))
    for index, sample in enumerate(samples):
        if index >= 6 * 24 and 6 <= sample.start.hour < 18:
            samples[index] = replace(
                sample, energy_mwh=18, intervals=(PowerInterval(sample.start, sample.end, 10),)
            )
    result = fit_calibration(tuple(samples), timezone="UTC", normalization_mw=100, limit_mw=None)
    assert result.status == "rejected"
    assert "baseline_bias_regression" in cast(list[str], result.report["reasons"])


def test_small_source_power_does_not_establish_training_leverage() -> None:
    result = fit_calibration(
        _samples(7, power=0.01), timezone="UTC", normalization_mw=100, limit_mw=None
    )
    assert result.status == "insufficient"
    support = cast(dict[str, object], result.report["training_support"])
    assert support["informative_days"] == 0
    daily = cast(list[dict[str, object]], support["day_support"])
    assert daily[0]["informative_hours"] == 12
    assert daily[0]["normalized_leverage_hours"] == pytest.approx(1.2e-7)


def test_training_support_requires_five_supported_days() -> None:
    samples = list(_samples(7))
    for index in range(24):
        sample = samples[index]
        power = 0.01 if 6 <= sample.start.hour < 18 else 0
        samples[index] = replace(
            sample,
            energy_mwh=power * 0.8,
            intervals=(PowerInterval(sample.start, sample.end, power),),
        )
    result = fit_calibration(tuple(samples), timezone="UTC", normalization_mw=100, limit_mw=None)
    assert result.status == "insufficient"
    assert cast(dict[str, object], result.report["training_support"])["informative_days"] == 4


def test_support_counts_positive_native_duration_and_excludes_always_clipped_power() -> None:
    samples = []
    for sample in _samples(7):
        middle = sample.start + timedelta(minutes=30)
        positive = sample.start.hour in (6, 7, 8)
        samples.append(
            replace(
                sample,
                energy_mwh=16 if positive else 0,
                intervals=(
                    PowerInterval(sample.start, middle, 40 if positive else 0),
                    PowerInterval(middle, sample.end, 0),
                ),
            )
        )
    result = fit_calibration(tuple(samples), timezone="UTC", normalization_mw=100, limit_mw=100)
    assert result.status == "insufficient"
    daily = cast(
        list[dict[str, object]],
        cast(dict[str, object], result.report["training_support"])["day_support"],
    )
    assert daily[0]["informative_hours"] == 1.5
    flat = fit_calibration(
        _samples(7, power=1000, factor=0.08),
        timezone="UTC",
        normalization_mw=100,
        limit_mw=100,
    )
    assert flat.status == "insufficient"


def test_rolling_origin_diagnostics_never_use_final_holdout() -> None:
    initial = fit_calibration(_samples(30), timezone="UTC", normalization_mw=100, limit_mw=100)
    samples = list(_samples(30))
    for index in range(23 * 24, len(samples)):
        samples[index] = replace(samples[index], energy_mwh=samples[index].energy_mwh * 1.5)
    changed = fit_calibration(tuple(samples), timezone="UTC", normalization_mw=100, limit_mw=100)
    assert initial.report["rolling_origin"] == changed.report["rolling_origin"]
    rolling = cast(dict[str, object], initial.report["rolling_origin"])
    assert rolling["status"] == "diagnostic"
    folds = cast(list[dict[str, object]], rolling["folds"])
    assert len(folds) == 3
    for fold in folds:
        training = cast(list[str], fold["training_day_dates"])
        evaluation = cast(list[str], fold["evaluation_day_dates"])
        assert max(training) < min(evaluation)
        assert len(evaluation) == 3
        assert max(evaluation) < "2026-06-24"


def test_rolling_origin_skips_unsupported_early_training_days() -> None:
    samples = list(_samples(30))
    for index in range(15 * 24):
        sample = samples[index]
        power = 0.01 if 6 <= sample.start.hour < 18 else 0
        samples[index] = replace(
            sample,
            energy_mwh=power * 0.8,
            intervals=(PowerInterval(sample.start, sample.end, power),),
        )
    result = fit_calibration(tuple(samples), timezone="UTC", normalization_mw=100, limit_mw=100)
    assert result.status == "ready"
    rolling = cast(dict[str, object], result.report["rolling_origin"])
    folds = cast(list[dict[str, object]], rolling["folds"])
    assert len(folds) == 1


def test_public_evaluation_preserves_units_and_excludes_partial_days() -> None:
    report = evaluate_samples(_samples(2)[:-1], timezone="UTC", factor=0.9, limit_mw=100)
    assert report["valid_days"] == 1
    assert report["samples"] == 24
    assert report["included_day_dates"] == ["2026-06-01"]
    metrics = cast(dict[str, float], report["metrics"])
    assert metrics["interval_mae_mw"] == pytest.approx(2)
    assert metrics["source_positive_mae_mw"] == pytest.approx(4)
    assert metrics["daily_mae_mwh"] == pytest.approx(48)
    assert report["day_errors"] == [
        {
            "day": "2026-06-01",
            "measured_energy_mwh": 384,
            "predicted_energy_mwh": 432,
            "error_mwh": 48,
        }
    ]
    empty = evaluate_samples((), timezone="UTC", limit_mw=None)
    assert empty["metrics"] is None
    assert empty["day_errors"] == []


@pytest.mark.parametrize("factor", [0, float("nan"), float("inf"), 0.1, 3])
def test_invalid_incumbent_factor_is_rejected(factor: float) -> None:
    with pytest.raises(ValueError, match="incumbent_factor"):
        fit_calibration(
            (), timezone="UTC", normalization_mw=100, limit_mw=None, incumbent_factor=factor
        )


def test_public_evaluation_validates_factor_and_cap() -> None:
    with pytest.raises(ValueError, match="factor"):
        evaluate_samples((), timezone="UTC", factor=0, limit_mw=None)
    with pytest.raises(ValueError, match="limit_mw"):
        evaluate_samples((), timezone="UTC", limit_mw=0)


@pytest.mark.parametrize(
    ("power", "energy", "error", "source_positive_mae"),
    [(10, 0, 240, 10), (0, 10, -240, None), (0, 0, 0, None)],
)
def test_evaluation_retains_complete_valid_zero_source_and_zero_meter_days(
    power: float, energy: float, error: float, source_positive_mae: float | None
) -> None:
    samples = tuple(
        replace(
            sample,
            energy_mwh=energy,
            intervals=(PowerInterval(sample.start, sample.end, power),),
        )
        for sample in _samples(1)
    )
    report = evaluate_samples(samples, timezone="UTC", limit_mw=None)
    assert report["valid_days"] == 1
    assert report["samples"] == 24
    metrics = cast(dict[str, float | None], report["metrics"])
    assert metrics["daily_bias_mwh"] == error
    assert metrics["daily_mae_mwh"] == abs(error)
    assert metrics["interval_mae_mw"] == abs(error) / 24
    assert metrics["source_positive_mae_mw"] == source_positive_mae
    daily = cast(list[dict[str, object]], report["day_errors"])
    assert daily[0]["predicted_energy_mwh"] == power * 24
    assert daily[0]["measured_energy_mwh"] == energy * 24
