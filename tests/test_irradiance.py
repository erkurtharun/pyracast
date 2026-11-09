from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from pyracast.irradiance import (
    MODEL_VERSION,
    BaselineConfiguration,
    ForecastVintage,
    IrradianceInterval,
    IrradianceSeries,
    PowerBasis,
    PowerInterval,
    SourceKind,
    Sunlight,
    estimate_intervals,
    integrate_day,
    local_day_bounds,
)

START = datetime(2026, 11, 10, tzinfo=UTC)
CONFIG = BaselineConfiguration(2, "ac", "ac", True)
VINTAGE = ForecastVintage("fixture", "forecast", START, None, "revision-1")


def series_with(intervals: tuple[IrradianceInterval, ...]) -> IrradianceSeries:
    return IrradianceSeries(intervals, CONFIG, VINTAGE)


@pytest.mark.parametrize("capacity", [0, -1, float("nan"), float("inf")])
def test_capacity_requires_positive_finite_kw(capacity: float) -> None:
    with pytest.raises(ValueError, match="Capacity"):
        replace(CONFIG, capacity_kw=capacity)


def test_baseline_requires_verified_matching_horizontal_configuration() -> None:
    with pytest.raises(ValueError, match="AC/DC"):
        replace(CONFIG, capacity_basis="dc")
    with pytest.raises(ValueError, match="AC/DC"):
        replace(CONFIG, power_basis=cast(PowerBasis, "unknown"))
    with pytest.raises(ValueError, match="verified"):
        replace(CONFIG, capacity_semantics_verified=False)
    with pytest.raises(ValueError, match="horizontal"):
        replace(CONFIG, tilt_degrees=1)
    assert replace(CONFIG, power_basis="dc", capacity_basis="dc").capacity_basis == "dc"


def test_vintage_preserves_issue_time_and_requires_metadata() -> None:
    assert VINTAGE.issued_at is None
    assert replace(VINTAGE, issued_at=START).issued_at == START
    assert replace(VINTAGE, source_kind="estimated_actuals").source_kind == "estimated_actuals"
    with pytest.raises(ValueError, match="required"):
        replace(VINTAGE, provider="")
    with pytest.raises(ValueError, match="required"):
        replace(VINTAGE, binding_revision="")
    with pytest.raises(ValueError, match="source kind"):
        replace(VINTAGE, source_kind=cast(SourceKind, "observation"))
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(VINTAGE, collected_at=START.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(VINTAGE, issued_at=START.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("power", "sunlight", "expected", "flag"),
    [
        (1.2, "day", 600.0, None),
        (1.2, "unknown", 600.0, "sunlight_unknown"),
        (1.2, "partial", 600.0, "partial_sunlight"),
        (0.0, "night", 0.0, None),
        (0.0, "day", None, "ambiguous_zero_power"),
        (0.0, "partial", None, "ambiguous_zero_power"),
        (0.0, "unknown", None, "ambiguous_zero_power"),
        (None, "day", None, "missing_power"),
        (float("nan"), "day", None, "nonfinite_power"),
        (float("inf"), "day", None, "nonfinite_power"),
        (-0.1, "day", None, "negative_power"),
        (0.1, "night", None, "night_power_conflict"),
        (1e308, "day", None, "nonfinite_irradiance"),
    ],
)
def test_baseline_values_and_quality(
    power: float | None, sunlight: Sunlight, expected: float | None, flag: str | None
) -> None:
    end = START + timedelta(minutes=30)
    result = estimate_intervals([PowerInterval(START, end, power, sunlight)], CONFIG, VINTAGE)
    interval = result.intervals[0]
    assert interval.ghi_wm2 == expected
    assert "unvalidated" in interval.quality_flags
    if flag is not None:
        assert flag in interval.quality_flags
    assert result.vintage is VINTAGE
    assert result.configuration is CONFIG
    assert result.model_version == MODEL_VERSION
    assert interval.start == START
    assert interval.end == end


def test_unknown_sunlight_is_rejected() -> None:
    interval = PowerInterval(START, START + timedelta(hours=1), 1, cast(Sunlight, "sunny"))
    with pytest.raises(ValueError, match="sunlight"):
        estimate_intervals([interval], CONFIG, VINTAGE)


@pytest.mark.parametrize("offset", [0, -1])
def test_empty_and_reversed_intervals_are_rejected(offset: int) -> None:
    with pytest.raises(ValueError, match="end"):
        estimate_intervals(
            [PowerInterval(START, START + timedelta(hours=offset), 1)], CONFIG, VINTAGE
        )


def test_naive_interval_and_duplicate_and_overlapping_intervals_are_rejected() -> None:
    interval = PowerInterval(START, START + timedelta(hours=1), 1)
    with pytest.raises(ValueError, match="timezone-aware"):
        estimate_intervals([replace(interval, start=START.replace(tzinfo=None))], CONFIG, VINTAGE)
    with pytest.raises(ValueError, match="overlapping"):
        estimate_intervals([interval, interval], CONFIG, VINTAGE)
    with pytest.raises(ValueError, match="overlapping"):
        estimate_intervals(
            [interval, replace(interval, start=START + timedelta(minutes=30))], CONFIG, VINTAGE
        )


def test_half_hour_energy_and_incomplete_total_and_provenance() -> None:
    series = estimate_intervals(
        [PowerInterval(START, START + timedelta(minutes=30), 1.2, "day")], CONFIG, VINTAGE
    )
    result = integrate_day(series, date(2026, 11, 10), "UTC")
    assert result.partial_energy_kwh_m2 == pytest.approx(0.3)
    assert result.partial_energy_mj_m2 == pytest.approx(1.08)
    assert result.energy_kwh_m2 is None
    assert result.energy_mj_m2 is None
    assert not result.complete
    assert result.coverage_seconds == result.valid_coverage_seconds == 1800
    assert result.coverage_fraction == pytest.approx(1 / 48)
    assert result.vintage is VINTAGE
    assert result.configuration is CONFIG
    assert result.model_version == MODEL_VERSION
    assert result.surface == "horizontal"
    assert result.intervals == series.intervals
    assert result.quality_flags == ("incomplete_day", "unvalidated")


@pytest.mark.parametrize(
    ("target", "zone", "start", "hours"),
    [
        (date(2026, 3, 8), "America/New_York", datetime(2026, 3, 8, 5, tzinfo=UTC), 23),
        (date(2026, 11, 1), "America/New_York", datetime(2026, 11, 1, 4, tzinfo=UTC), 25),
        (date(2026, 11, 10), "Europe/Istanbul", datetime(2026, 11, 9, 21, tzinfo=UTC), 24),
    ],
)
def test_full_local_day_uses_actual_dst_duration(
    target: date, zone: str, start: datetime, hours: int
) -> None:
    end = start + timedelta(hours=hours)
    assert local_day_bounds(target, zone) == (start, end)
    result = integrate_day(
        series_with((IrradianceInterval(start, end, 1000, ("unvalidated",)),)), target, zone
    )
    assert result.complete
    assert result.energy_kwh_m2 == hours
    assert result.energy_mj_m2 == pytest.approx(hours * 3.6)
    assert result.coverage_fraction == 1
    assert result.coverage_seconds == hours * 3600
    assert "incomplete_day" not in result.quality_flags


def test_skipped_civil_date_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive duration"):
        local_day_bounds(date(2011, 12, 30), "Pacific/Apia")


def test_boundary_prorating_and_adjacent_unsorted_intervals_conserve_energy() -> None:
    day_end = START + timedelta(days=1)
    series = series_with(
        (
            IrradianceInterval(
                day_end - timedelta(minutes=15), day_end + timedelta(minutes=15), 600, ()
            ),
            IrradianceInterval(
                START - timedelta(minutes=15), START + timedelta(minutes=15), 600, ()
            ),
            IrradianceInterval(
                START + timedelta(minutes=15), day_end - timedelta(minutes=15), 1000, ()
            ),
            IrradianceInterval(
                START - timedelta(days=1), START - timedelta(minutes=15), None, ("ignored",)
            ),
            IrradianceInterval(
                day_end + timedelta(minutes=15), day_end + timedelta(days=1), None, ("ignored",)
            ),
        )
    )
    result = integrate_day(series, START.date(), "UTC")
    assert result.complete
    assert result.energy_kwh_m2 == pytest.approx(23.8)
    assert result.coverage_seconds == result.valid_coverage_seconds == 86400
    assert "prorated_interval_mean" in result.quality_flags
    assert "ignored" not in result.quality_flags
    assert len(result.intervals) == 3
    assert result.intervals[0].start < result.start
    assert result.intervals[-1].end > result.end


def test_missing_daylight_is_not_zero_even_with_full_source_coverage() -> None:
    end = START + timedelta(days=1)
    result = integrate_day(
        series_with(
            (
                IrradianceInterval(START, START + timedelta(hours=12), 0, ()),
                IrradianceInterval(START + timedelta(hours=12), end, None, ("missing_power",)),
            )
        ),
        START.date(),
        "UTC",
    )
    assert result.coverage_seconds == 86400
    assert result.valid_coverage_seconds == 43200
    assert result.coverage_fraction == 0.5
    assert result.partial_energy_kwh_m2 == 0
    assert result.energy_kwh_m2 is None
    assert "missing_power" in result.quality_flags


def test_empty_day_has_no_total() -> None:
    result = integrate_day(estimate_intervals([], CONFIG, VINTAGE), START.date(), "UTC")
    assert result.energy_kwh_m2 is None
    assert result.coverage_fraction == result.partial_energy_kwh_m2 == 0


@pytest.mark.parametrize("ghi", [-1, float("nan"), float("inf")])
def test_direct_series_cannot_bypass_irradiance_validation(ghi: float) -> None:
    with pytest.raises(ValueError, match="Irradiance"):
        integrate_day(
            series_with((IrradianceInterval(START, START + timedelta(days=1), ghi, ()),)),
            START.date(),
            "UTC",
        )


def test_direct_series_cannot_bypass_overlap_validation() -> None:
    interval = IrradianceInterval(START, START + timedelta(hours=1), 1, ())
    with pytest.raises(ValueError, match="overlapping"):
        integrate_day(series_with((interval, interval)), START.date(), "UTC")


def test_fall_back_fold_intervals_are_compared_in_utc() -> None:
    zone = ZoneInfo("America/New_York")
    first = datetime(2026, 11, 1, 1, tzinfo=zone, fold=0)
    second = datetime(2026, 11, 1, 1, tzinfo=zone, fold=1)
    series = estimate_intervals([PowerInterval(first, second, 1.2, "day")], CONFIG, VINTAGE)
    result = integrate_day(series, date(2026, 11, 1), "America/New_York")
    assert result.valid_coverage_seconds == 3600
    assert result.partial_energy_kwh_m2 == pytest.approx(0.6)


def test_fractional_seconds_do_not_make_complete_coverage_inexact() -> None:
    cut = START + timedelta(microseconds=333333)
    end = START + timedelta(days=1)
    result = integrate_day(
        series_with(
            (
                IrradianceInterval(START, cut, 1000, ()),
                IrradianceInterval(cut, end, 1000, ()),
            )
        ),
        START.date(),
        "UTC",
    )
    assert result.complete
    assert result.coverage_fraction == 1
    assert result.energy_kwh_m2 == pytest.approx(24)
