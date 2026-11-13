from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from pyracast.plants.numerics import (
    DERATING_POLICY,
    CalibrationSample,
    PowerInterval,
    fit_calibration,
)

START = datetime(2026, 6, 1, tzinfo=UTC)


def _samples(
    days: int,
    performance: Callable[[int], float],
    *,
    power: Callable[[int, int], float] = lambda _day, _hour: 60.0,
    factor: float = 0.8,
    cap: float | None = None,
) -> tuple[CalibrationSample, ...]:
    result = []
    for day in range(days):
        for hour in range(24):
            start = START + timedelta(days=day, hours=hour)
            end = start + timedelta(hours=1)
            native = power(day, hour) if 6 <= hour < 18 else 0.0
            energy = native * factor * performance(day)
            result.append(
                CalibrationSample(
                    start,
                    end,
                    energy if cap is None else min(cap, energy),
                    (PowerInterval(start, end, native),),
                )
            )
    return tuple(result)


def _derating(
    samples: tuple[CalibrationSample, ...], *, incumbent: float = 1.0, cap: float | None = None
) -> dict[str, Any]:
    result = fit_calibration(
        samples,
        timezone="UTC",
        normalization_mw=100,
        limit_mw=cap,
        incumbent_factor=incumbent,
    )
    return cast(dict[str, Any], result.report["derating"])


def test_recent_shortfall_against_the_active_factor_is_suspected() -> None:
    assessment = _derating(_samples(14, lambda day: 0.85 if day >= 7 else 1.0), incumbent=0.8)
    assert assessment["policy"] == DERATING_POLICY
    assert assessment["status"] == "suspected"
    assert assessment["baseline_factor"] == 0.8
    assert assessment["performance_ratio"] == pytest.approx(0.85)
    assert assessment["window_start"] == "2026-06-05"
    assert assessment["window_end"] == "2026-06-14"
    assert [day["day"] for day in assessment["days"]] == [
        f"2026-06-{day:02d}" for day in range(5, 15)
    ]
    assert assessment["days"][0]["performance_index"] == pytest.approx(1.0)
    assert assessment["days"][-1]["hours"] == 12
    assert assessment["limit_reached"] is False


def test_small_or_brief_shortfalls_stay_normal() -> None:
    mild = _derating(_samples(14, lambda day: 0.95 if day >= 7 else 1.0), incumbent=0.8)
    assert mild["status"] == "normal"
    assert mild["performance_ratio"] == pytest.approx(0.95)
    brief = _derating(_samples(14, lambda day: 0.5 if day >= 12 else 1.0), incumbent=0.8)
    assert brief["status"] == "normal"
    assert brief["performance_ratio"] == pytest.approx(1.0)


def test_accepted_candidate_is_the_baseline_without_an_active_calibration() -> None:
    assessment = _derating(_samples(12, lambda _day: 1.0))
    assert assessment["status"] == "normal"
    assert assessment["baseline_factor"] == pytest.approx(0.8, abs=5e-3)
    assert assessment["performance_ratio"] == pytest.approx(1.0, abs=5e-3)


def test_no_assessment_without_an_applied_baseline() -> None:
    rejected = fit_calibration(
        _samples(12, lambda _day: 1.0, factor=1.0),
        timezone="UTC",
        normalization_mw=100,
        limit_mw=None,
    )
    assert rejected.status == "rejected"
    assert rejected.report["derating"] == {"policy": DERATING_POLICY, "status": "not_evaluated"}
    insufficient = fit_calibration(
        _samples(3, lambda _day: 1.0), timezone="UTC", normalization_mw=100, limit_mw=None
    )
    assert insufficient.report["derating"]["status"] == "not_evaluated"  # type: ignore[index]


def test_weak_days_and_short_strong_periods_do_not_qualify() -> None:
    def power(day: int, hour: int) -> float:
        if day >= 11:
            return 20.0
        return 60.0 if day < 9 or hour < 8 else 20.0

    assessment = _derating(_samples(14, lambda _day: 0.5, power=power), incumbent=0.8)
    assert [day["day"] for day in assessment["days"]] == [
        f"2026-06-{day:02d}" for day in range(5, 10)
    ]
    weak = _derating(_samples(14, lambda _day: 0.5, power=lambda _day, _hour: 20.0), incumbent=0.8)
    assert weak["status"] == "insufficient"
    assert weak["performance_ratio"] is None
    assert weak["days"] == []


def test_clipped_intervals_are_excluded_and_reported() -> None:
    def power(_day: int, hour: int) -> float:
        return 80.0 if hour in (11, 12) else 50.0

    assessment = _derating(
        _samples(14, lambda day: 0.85 if day >= 7 else 1.0, power=power, cap=50.0),
        incumbent=0.8,
        cap=50.0,
    )
    assert assessment["limit_reached"] is True
    assert assessment["status"] == "suspected"
    assert assessment["performance_ratio"] == pytest.approx(0.85)
    assert assessment["days"][-1]["hours"] == 10
