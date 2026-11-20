from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest

from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.composition import ForecastVintage, compose_forecasts
from pyracast.intervals import NormalizedInterval
from pyracast.irradiance import local_day_bounds
from pyracast.plants.numerics import PowerInterval, forecast
from pyracast.plants.operating_limits import OperatingLimit, affected_power
from pyracast.solar import Daylight, daylight_energy_windows, daylight_window, summarize_daylight


@pytest.fixture
def nigde_binding() -> SiteBinding:
    return SiteBinding(
        id=7,
        region_id=8,
        region_revision=1,
        provider_account=ProviderAccount(provider="solcast"),
        capacity_kw=1000,
        capacity_semantics_verified=True,
        latitude=37.80375,
        longitude=34.460913,
        timezone="Europe/Istanbul",
    )


@pytest.mark.parametrize("all_zero", [False, True])
def test_nigde_separates_daylight_validity_from_source_and_night_gaps(
    all_zero: bool, nigde_binding: SiteBinding
) -> None:
    start = datetime(2026, 11, 14, 0, 30, tzinfo=UTC)
    rows: list[dict[str, object]] = [
        {
            "start": (start + timedelta(minutes=30 * offset)).isoformat(),
            "end": (start + timedelta(minutes=30 * (offset + 1))).isoformat(),
            "power_kw": 100
            if not all_zero and 4 <= (start + timedelta(minutes=30 * offset)).hour < 15
            else 0,
        }
        for offset in range(41)
    ]
    result = calculate(
        nigde_binding,
        date(2026, 11, 14),
        ProviderData(
            {}, NormalizedInterval.from_rows(rows), collected_at=start + timedelta(minutes=8)
        ),
    )
    assert result["complete"] is False
    assert result["energy_kwh_m2"] is None
    daylight = cast(dict[str, Any], result["daylight"])
    assert daylight["complete"] is not all_zero
    assert daylight["coverage_fraction"] == (0 if all_zero else 1)
    assert daylight["coverage_seconds"] == daylight["duration_seconds"]
    assert daylight["input_coverage_seconds"] == daylight["duration_seconds"]
    assert daylight["input_coverage_fraction"] == 1
    assert daylight["energy_kwh_m2"] == (None if all_zero else pytest.approx(1.1))
    assert daylight["sunrise"].startswith("2026-11-14T04:")
    assert daylight["sunset"].startswith("2026-11-14T14:")
    assert result["intervals"] == [
        {
            "start": row["start"],
            "end": row["end"],
            "ghi_wm2": 100 if row["power_kw"] else None,
            "quality_flags": ["unvalidated", "sunlight_unknown"]
            if row["power_kw"]
            else ["unvalidated", "sunlight_unknown", "ambiguous_zero_power"],
        }
        for row in rows
    ]


@pytest.mark.parametrize("missing", [None, -1, float("nan"), float("inf"), "absent"])
def test_irradiance_input_coverage_excludes_missing_or_invalid_power(
    missing: float | str | None, nigde_binding: SiteBinding
) -> None:
    start = datetime(2026, 11, 14, tzinfo=UTC)
    rows: list[dict[str, object]] = [
        {
            "start": (start + timedelta(minutes=30 * index)).isoformat(),
            "end": (start + timedelta(minutes=30 * (index + 1))).isoformat(),
            "power_kw": missing if index == 24 else 100,
        }
        for index in range(48)
        if not (index == 24 and missing == "absent")
    ]
    result = calculate(
        nigde_binding,
        start.date(),
        ProviderData({}, NormalizedInterval.from_rows(rows), collected_at=start),
    )
    summary = cast(dict[str, Any], result["daylight"])
    assert summary["input_coverage_seconds"] == pytest.approx(36601.476925 - 1800)
    assert summary["input_coverage_fraction"] == pytest.approx(1 - 1800 / 36601.476925)
    assert summary["complete"] is False
    assert summary["energy_kwh_m2"] is None
    assert summary["partial_energy_kwh_m2"] == pytest.approx(1.05)


def test_irradiance_zero_boundary_power_keeps_complete_input_and_partial_ghi(
    nigde_binding: SiteBinding,
) -> None:
    start = datetime(2026, 11, 14, 4, tzinfo=UTC)
    rows: list[dict[str, object]] = [
        {
            "start": (start + timedelta(minutes=30 * index)).isoformat(),
            "end": (start + timedelta(minutes=30 * (index + 1))).isoformat(),
            "power_kw": 0 if index in (0, 21) else 100,
        }
        for index in range(22)
    ]
    result = calculate(
        nigde_binding,
        start.date(),
        ProviderData({}, NormalizedInterval.from_rows(rows), collected_at=start),
    )
    summary = cast(dict[str, Any], result["daylight"])
    assert summary["input_coverage_seconds"] == pytest.approx(36601.476925)
    assert summary["input_coverage_fraction"] == 1
    assert summary["valid_coverage_seconds"] == 36000
    assert summary["coverage_fraction"] == pytest.approx(0.9835668673635087)
    assert summary["complete"] is False
    assert summary["energy_kwh_m2"] is None
    assert summary["partial_energy_kwh_m2"] == pytest.approx(1)


def test_irradiance_input_coverage_uses_owned_pieces_and_preserves_authoritative_gaps(
    nigde_binding: SiteBinding,
) -> None:
    start = datetime(2026, 11, 14, tzinfo=UTC)
    end = start + timedelta(days=1)
    recent_start, recent_end = start + timedelta(hours=10), start + timedelta(hours=11)
    composition = compose_forecasts(
        (
            ForecastVintage(
                2,
                recent_start,
                recent_end,
                (
                    {
                        "start": recent_start.isoformat(),
                        "end": (recent_start + timedelta(minutes=30)).isoformat(),
                        "power_kw": None,
                    },
                    {
                        "start": (recent_start + timedelta(minutes=45)).isoformat(),
                        "end": recent_end.isoformat(),
                        "power_kw": 0,
                    },
                ),
            ),
            ForecastVintage(
                1,
                start,
                end,
                ({"start": start.isoformat(), "end": end.isoformat(), "power_kw": 100},),
            ),
        ),
        start,
        end,
    )
    result = calculate(
        nigde_binding,
        start.date(),
        ProviderData({}, NormalizedInterval.from_rows(composition.intervals), collected_at=start),
    )
    summary = cast(dict[str, Any], result["daylight"])
    assert summary["input_coverage_seconds"] == pytest.approx(36601.476925 - 2700)
    assert summary["input_coverage_fraction"] == pytest.approx(1 - 2700 / 36601.476925)
    assert summary["valid_coverage_seconds"] == pytest.approx(36601.476925 - 3600)
    assert summary["energy_kwh_m2"] is None
    assert summary["partial_energy_kwh_m2"] == pytest.approx(2)


def test_irradiance_polar_night_has_no_input_coverage_fraction(nigde_binding: SiteBinding) -> None:
    nigde_binding.latitude = 90
    start = datetime(2026, 12, 21, tzinfo=UTC)
    result = calculate(
        nigde_binding,
        start.date(),
        ProviderData(
            {},
            NormalizedInterval.from_rows(
                [
                    {
                        "start": (start - timedelta(days=1)).isoformat(),
                        "end": (start + timedelta(days=1)).isoformat(),
                        "power_kw": 0,
                    }
                ]
            ),
            collected_at=start,
        ),
    )
    summary = cast(dict[str, Any], result["daylight"])
    assert summary["input_coverage_seconds"] == 0
    assert summary["input_coverage_fraction"] is None
    assert summary["coverage_fraction"] is None
    assert summary["energy_kwh_m2"] is None
    assert summary["complete"] is False


@pytest.mark.parametrize("resolution", [30, 60])
def test_nigde_intraday_source_has_complete_generation_despite_missing_night(
    resolution: int,
) -> None:
    day = date(2026, 11, 14)
    start, end = local_day_bounds(day, "Europe/Istanbul")
    daylight = daylight_window(day, "Europe/Istanbul", 37.80375, 34.460913)
    intervals = tuple(
        PowerInterval(
            start + timedelta(minutes=30 * index),
            start + timedelta(minutes=30 * (index + 1)),
            (80 if index % 2 == 0 else 120) if 14 <= index < 36 else 0,
        )
        for index in range(7, 48)
    )
    result = forecast(
        intervals,
        start=start,
        end=end,
        resolution_minutes=resolution,
        limit_mw=100,
        daylight=daylight,
    )
    summary = cast(dict[str, Any], result["daylight"])
    assert result["energy_mwh"] is None
    assert result["coverage_fraction"] == pytest.approx(41 / 48)
    assert result["partial_energy_mwh"] == 990
    assert summary["complete"] is True
    assert summary["coverage_fraction"] == 1
    assert summary["energy_mwh"] == 990
    assert "input_coverage_fraction" not in summary
    assert "input_coverage_seconds" not in summary


@pytest.mark.parametrize("resolution", [30, 60])
def test_night_gaps_keep_native_clipping_and_boundary_energy(resolution: int) -> None:
    start = datetime(2026, 11, 14, tzinfo=UTC)
    end = start + timedelta(days=1)
    rise, setting = start + timedelta(hours=6, minutes=15), start + timedelta(hours=17, minutes=45)
    daylight = Daylight(((rise, setting),), rise, setting, "normal")
    intervals = tuple(
        PowerInterval(
            start + timedelta(minutes=30 * index),
            start + timedelta(minutes=30 * (index + 1)),
            80 if index % 2 == 0 else 120,
        )
        for index in range(12, 36)
    )
    result = forecast(
        intervals,
        start=start,
        end=end,
        resolution_minutes=resolution,
        limit_mw=100,
        daylight=daylight,
    )
    summary = cast(dict[str, Any], result["daylight"])
    assert result["energy_mwh"] is None
    assert result["coverage_fraction"] == 0.5
    assert summary["energy_mwh"] == 1080
    assert summary["coverage_fraction"] == 1
    assert summary["duration_seconds"] == 11.5 * 3600
    assert summary["energy_scope"] == "whole_native_intervals"
    assert cast(list[dict[str, Any]], result["intervals"])[0]["mean_power_mw"] is None


@pytest.mark.parametrize("gap", [6, 12, 17])
@pytest.mark.parametrize("missing_row", [False, True])
def test_daylight_boundary_and_internal_gaps_remain_incomplete(gap: int, missing_row: bool) -> None:
    start = datetime(2026, 11, 14, tzinfo=UTC)
    rise, setting = start + timedelta(hours=6, minutes=15), start + timedelta(hours=17, minutes=45)
    daylight = Daylight(((rise, setting),), rise, setting, "normal")
    intervals = tuple(
        PowerInterval(
            start + timedelta(hours=index),
            start + timedelta(hours=index + 1),
            None if index == gap else 10,
        )
        for index in range(6, 18)
        if not (missing_row and index == gap)
    )
    result = forecast(
        intervals,
        start=start,
        end=start + timedelta(days=1),
        resolution_minutes=60,
        limit_mw=None,
        daylight=daylight,
    )
    summary = cast(dict[str, Any], result["daylight"])
    assert summary["complete"] is False
    assert summary["energy_mwh"] is None
    assert summary["partial_energy_mwh"] == 110
    assert summary["coverage_fraction"] < 1
    assert (summary["coverage_seconds"] == summary["duration_seconds"]) is not missing_row


def test_dark_intervals_touching_daylight_do_not_enter_energy_and_zero_is_valid() -> None:
    start = datetime(2026, 11, 14, tzinfo=UTC)
    rise, setting = start + timedelta(hours=6), start + timedelta(hours=18)
    daylight = Daylight(((rise, setting),), rise, setting, "normal")
    intervals = (
        PowerInterval(start, rise, 7),
        PowerInterval(rise, setting, 0),
        PowerInterval(setting, start + timedelta(days=1), 7),
    )
    result = forecast(
        intervals,
        start=start,
        end=start + timedelta(days=1),
        resolution_minutes=60,
        limit_mw=None,
        daylight=daylight,
    )
    assert result["energy_mwh"] == 84
    summary = cast(dict[str, Any], result["daylight"])
    assert summary["complete"] is True
    assert summary["energy_mwh"] == 0


def test_outage_fragments_keep_whole_native_boundary_scope() -> None:
    start = datetime(2026, 11, 14, 6, tzinfo=UTC)
    end = start + timedelta(hours=1)
    rise = start + timedelta(minutes=20)
    daylight = Daylight(((rise, end),), rise, end, "normal")
    native = (PowerInterval(start, end, 100),)
    windows = daylight_energy_windows(daylight, ((start, end),))
    pieces = affected_power(
        native,
        (OperatingLimit(start + timedelta(minutes=10), None, 50, None, None),),
        dc_capacity_mw=100,
        normal_limit_mw=100,
        factor=1,
        net_export=True,
    )
    result = forecast(
        pieces,
        start=start,
        end=end,
        resolution_minutes=30,
        limit_mw=None,
        daylight=daylight,
        energy_windows=windows,
    )
    summary = cast(dict[str, Any], result["daylight"])
    assert summary["complete"] is True
    assert summary["energy_mwh"] == pytest.approx(100 / 6 + 50 * 5 / 6)
    assert summary["energy_mwh"] == pytest.approx(result["energy_mwh"])


def test_unknown_outage_before_sunrise_within_native_boundary_keeps_total_missing() -> None:
    start = datetime(2026, 11, 14, 6, tzinfo=UTC)
    end = start + timedelta(hours=1)
    rise = start + timedelta(minutes=20)
    daylight = Daylight(((rise, end),), rise, end, "normal")
    windows = daylight_energy_windows(daylight, ((start, end),))
    pieces = affected_power(
        (PowerInterval(start, end, 100),),
        (OperatingLimit(start, start + timedelta(minutes=10), None, None, None),),
        dc_capacity_mw=100,
        normal_limit_mw=100,
        factor=1,
        net_export=True,
    )
    result = forecast(
        pieces,
        start=start,
        end=end,
        resolution_minutes=60,
        limit_mw=None,
        daylight=daylight,
        energy_windows=windows,
    )
    summary = cast(dict[str, Any], result["daylight"])
    assert summary["coverage_fraction"] == 1
    assert summary["complete"] is False
    assert summary["energy_mwh"] is None
    assert summary["partial_energy_mwh"] == pytest.approx(100 * 5 / 6)
    assert "input_coverage_fraction" not in summary
    assert "input_coverage_seconds" not in summary


def test_missing_original_native_boundary_piece_does_not_become_complete() -> None:
    start = datetime(2026, 11, 14, 6, tzinfo=UTC)
    end = start + timedelta(hours=1)
    rise = start + timedelta(minutes=20)
    daylight = Daylight(((rise, end),), rise, end, "normal")
    result = forecast(
        (PowerInterval(start + timedelta(minutes=10), end, 100),),
        start=start,
        end=end,
        resolution_minutes=60,
        limit_mw=None,
        daylight=daylight,
        energy_windows=daylight_energy_windows(daylight, ((start, end),)),
    )
    summary = cast(dict[str, Any], result["daylight"])
    assert summary["coverage_fraction"] == 1
    assert summary["complete"] is False
    assert summary["energy_mwh"] is None


@pytest.mark.parametrize(
    ("day", "latitude", "hours"), [(date(2026, 3, 29), 90, 23), (date(2026, 10, 25), -90, 25)]
)
def test_polar_day_uses_elapsed_dst_hours(day: date, latitude: float, hours: int) -> None:
    start, end = local_day_bounds(day, "Europe/Berlin")
    daylight = daylight_window(day, "Europe/Berlin", latitude, 13.405)
    result = forecast(
        (PowerInterval(start, end, 3),),
        start=start,
        end=end,
        resolution_minutes=60,
        limit_mw=None,
        daylight=daylight,
    )
    summary = cast(dict[str, Any], result["daylight"])
    assert summary["status"] == "polar_day"
    assert summary["duration_seconds"] == hours * 3600
    assert summary["energy_mwh"] == 3 * hours
    assert summary["coverage_fraction"] == 1


def test_polar_night_and_unobserved_daylight_never_claim_complete_zero() -> None:
    start = datetime(2026, 12, 21, tzinfo=UTC)
    for latitude in (90, 37.80375):
        daylight = daylight_window(start.date(), "UTC", latitude, 34.460913)
        result = forecast(
            (),
            start=start,
            end=start + timedelta(days=1),
            resolution_minutes=60,
            limit_mw=None,
            daylight=daylight,
        )
        summary = cast(dict[str, Any], result["daylight"])
        assert summary["complete"] is False
        assert summary["energy_mwh"] is None
        assert summary["partial_energy_mwh"] == 0
        assert summary["coverage_fraction"] == (None if latitude == 90 else 0)


def test_midnight_crossing_daylight_counts_each_native_interval_once() -> None:
    start = datetime(2026, 6, 21, tzinfo=UTC)
    end = start + timedelta(days=1)
    daylight = daylight_window(start.date(), "UTC", 65, 25)
    assert len(daylight.windows) == 2
    result = forecast(
        (PowerInterval(start, end, 2),),
        start=start,
        end=end,
        resolution_minutes=60,
        limit_mw=None,
        daylight=daylight,
    )
    summary = cast(dict[str, Any], result["daylight"])
    assert summary["energy_mwh"] == 48
    assert summary["coverage_fraction"] == 1


def test_native_window_union_and_day_clipping_prevent_duplicate_energy() -> None:
    start = datetime(2026, 11, 14, tzinfo=UTC)
    end = start + timedelta(days=1)
    daylight = Daylight(((start, end),), None, None, "polar_day")
    windows = daylight_energy_windows(
        daylight, ((start - timedelta(hours=1), end), (start, end + timedelta(hours=1)))
    )
    assert windows == ((start - timedelta(hours=1), end + timedelta(hours=1)),)
    summary = summarize_daylight(
        daylight,
        (
            (start - timedelta(hours=2), start - timedelta(hours=1), 10),
            (start - timedelta(hours=1), end + timedelta(hours=1), 2),
        ),
        start=start,
        end=end,
        energy_windows=windows,
        unit="mwh",
        input_windows=((start - timedelta(hours=1), end), (start, end + timedelta(hours=1))),
    )
    assert summary["energy_mwh"] == 48
    assert summary["input_coverage_seconds"] == 86400
    assert summary["input_coverage_fraction"] == 1
    with pytest.raises(ValueError, match="finite"):
        summarize_daylight(
            daylight,
            ((start, end, 1e308),),
            start=start,
            end=end,
            energy_windows=windows,
            unit="mwh",
        )


@pytest.mark.parametrize("input_hour", [7, 8, 10, 11])
def test_daylight_input_outside_summary_bounds_has_zero_coverage(input_hour: int) -> None:
    midnight = datetime(2026, 11, 14, tzinfo=UTC)
    rise, setting = midnight + timedelta(hours=6), midnight + timedelta(hours=18)
    start, end = midnight + timedelta(hours=9), midnight + timedelta(hours=10)
    input_start = midnight + timedelta(hours=input_hour)
    summary = summarize_daylight(
        Daylight(((rise, setting),), rise, setting, "normal"),
        ((start, end, 1),),
        start=start,
        end=end,
        energy_windows=((start, end),),
        unit="mwh",
        input_windows=((input_start, input_start + timedelta(hours=1)),),
    )
    assert summary["input_coverage_seconds"] == 0
    assert summary["input_coverage_fraction"] == 0
    assert summary["partial_energy_mwh"] == 1
