from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from pyracast.forecasts.composition import ForecastVintage, compose_forecasts
from pyracast.forecasts.faults import DamagedStoredDataError

START = datetime(2026, 11, 11, tzinfo=UTC)
NULL_ROW = cast(tuple[dict[str, Any], ...], (None,))


def _at(hours: float) -> datetime:
    return START + timedelta(hours=hours)


def _row(start: float, end: float, power: float | None) -> dict[str, Any]:
    return {"start": _at(start).isoformat(), "end": _at(end).isoformat(), "power_kw": power}


def _vintage(snapshot_id: int, start: float, end: float, *rows: dict[str, Any]) -> ForecastVintage:
    return ForecastVintage(snapshot_id, _at(start), _at(end), tuple(rows))


def _energy(rows: tuple[dict[str, Any], ...]) -> float:
    return sum(
        float(row["power_kw"])
        * (
            datetime.fromisoformat(row["end"]) - datetime.fromisoformat(row["start"])
        ).total_seconds()
        / 3600
        for row in rows
        if row["power_kw"] is not None
    )


def test_intraday_forecast_retains_earlier_native_daylight_from_previous_vintage() -> None:
    latest = _vintage(
        8,
        10.5,
        24,
        *(_row(index / 2, (index + 1) / 2, 4 if index < 35 else 0) for index in range(21, 48)),
    )
    previous = _vintage(
        6,
        0,
        24,
        *(_row(index / 2, (index + 1) / 2, 2 if 14 <= index < 35 else 0) for index in range(48)),
    )
    result = compose_forecasts((latest, previous), _at(0), _at(24))
    assert len(result.intervals) == 48
    assert [row["source_snapshot_id"] for row in result.intervals] == [6] * 21 + [8] * 27
    assert _energy(result.intervals) == 35
    assert result.snapshot_ids == (8, 6)
    assert result.segments == [
        {"start": _at(0).isoformat(), "end": _at(10.5).isoformat(), "snapshot_id": 6},
        {"start": _at(10.5).isoformat(), "end": _at(24).isoformat(), "snapshot_id": 8},
    ]
    assert result.prorated is False
    assert "source_snapshot_id" not in previous.intervals[0]


def test_older_envelope_fills_both_edges_and_preserves_native_means_and_metadata() -> None:
    native = {**_row(0, 4, 8), "ghi_wm2": 600, "quality_flags": ["provider"]}
    result = compose_forecasts(
        (_vintage(1, 1, 2, _row(1, 2, 4)), _vintage(99, 0, 4, native)), _at(0), _at(4)
    )
    assert result.intervals == (
        {
            **native,
            "end": _at(1).isoformat(),
            "original_start": _at(0).isoformat(),
            "original_end": _at(4).isoformat(),
            "source_snapshot_id": 99,
        },
        {**_row(1, 2, 4), "source_snapshot_id": 1},
        {
            **native,
            "start": _at(2).isoformat(),
            "original_start": _at(0).isoformat(),
            "original_end": _at(4).isoformat(),
            "source_snapshot_id": 99,
        },
    )
    assert result.snapshot_ids == (1, 99)
    assert result.prorated is True
    assert _energy(result.intervals) == 28


def test_provider_clipped_native_mean_retains_proration_without_another_split() -> None:
    row = {
        **_row(0, 0.25, 8),
        "original_start": _at(0).isoformat(),
        "original_end": _at(0.5).isoformat(),
    }
    result = compose_forecasts((_vintage(1, 0, 0.25, row),), _at(0), _at(0.25))
    assert result.prorated is True
    assert result.intervals == ({**row, "source_snapshot_id": 1},)
    assert _energy(result.intervals) == 2


def test_newer_null_internal_gap_and_zero_remain_authoritative() -> None:
    result = compose_forecasts(
        (
            _vintage(4, 1, 3, _row(1, 1.5, None), _row(2, 3, 0)),
            _vintage(5, 0, 4, _row(0, 4, 10)),
        ),
        _at(0),
        _at(4),
    )
    assert [row["power_kw"] for row in result.intervals] == [10, None, 0, 10]
    assert [(row["start"], row["end"]) for row in result.intervals] == [
        (_at(0).isoformat(), _at(1).isoformat()),
        (_at(1).isoformat(), _at(1.5).isoformat()),
        (_at(2).isoformat(), _at(3).isoformat()),
        (_at(3).isoformat(), _at(4).isoformat()),
    ]
    assert _energy(result.intervals) == 20


def test_misaligned_half_hour_and_hourly_intervals_conserve_overlap_energy() -> None:
    result = compose_forecasts(
        (
            _vintage(1, 1.25, 2.75, *(_row(h, h + 0.5, 20) for h in (1.25, 1.75, 2.25))),
            _vintage(2, 0, 4, *(_row(h, h + 1, 10) for h in range(4))),
        ),
        _at(0.5),
        _at(3.5),
    )
    assert [row["power_kw"] for row in result.intervals] == [10, 10, 20, 20, 20, 10, 10]
    assert _energy(result.intervals) == 45
    assert result.prorated is True
    assert (
        sum(
            (
                datetime.fromisoformat(row["end"]) - datetime.fromisoformat(row["start"])
            ).total_seconds()
            for row in result.intervals
        )
        == 3 * 3600
    )


def test_adjacent_coverage_ignores_older_boundaries_inside_newer_envelope() -> None:
    result = compose_forecasts(
        (
            _vintage(1, 1, 3, _row(1, 3, 3)),
            _vintage(2, 0, 2, _row(0, 1, 2), _row(1, 2, 2)),
            _vintage(3, 3, 4, _row(3, 4, 1)),
        ),
        _at(0),
        _at(4),
    )
    assert [row["source_snapshot_id"] for row in result.intervals] == [2, 1, 3]
    assert result.prorated is False
    assert len(result.segments) == 3


def test_empty_coverage_owns_missingness_and_unused_vintages_are_excluded() -> None:
    result = compose_forecasts(
        (
            _vintage(1, 1, 3),
            _vintage(2, 0, 4, _row(0, 4, 2)),
            _vintage(3, 0, 4, _row(0, 4, 3)),
            _vintage(4, 5, 6, _row(5, 6, 4)),
        ),
        _at(1),
        _at(3),
    )
    assert result.intervals == ()
    assert result.snapshot_ids == (1,)
    assert result.segments == [
        {"start": _at(1).isoformat(), "end": _at(3).isoformat(), "snapshot_id": 1}
    ]
    assert result.prorated is False


@pytest.mark.parametrize("vintages", [(), (_vintage(1, 3, 4, _row(3, 4, 1)),)])
def test_no_overlapping_coverage_stays_missing(vintages: tuple[ForecastVintage, ...]) -> None:
    result = compose_forecasts(vintages, _at(0), _at(2))
    assert result.intervals == ()
    assert result.segments == []
    assert result.snapshot_ids == ()
    assert result.prorated is False


def test_disjoint_coverage_preserves_unowned_gap() -> None:
    result = compose_forecasts(
        (_vintage(1, 0, 1, _row(0, 1, 1)), _vintage(2, 2, 3, _row(2, 3, 2))),
        _at(0),
        _at(3),
    )
    assert [row["power_kw"] for row in result.intervals] == [1, 2]
    assert len(result.segments) == 2


def test_unsorted_native_rows_are_selected_chronologically() -> None:
    result = compose_forecasts((_vintage(1, 0, 2, _row(1, 2, 2), _row(0, 1, 1)),), _at(0), _at(2))
    assert [row["power_kw"] for row in result.intervals] == [1, 2]


def test_daylight_saving_fold_uses_elapsed_time_and_retains_native_offsets() -> None:
    zone = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 10, 25, 2, tzinfo=zone, fold=0)
    end = datetime(2026, 10, 25, 2, tzinfo=zone, fold=1)
    native = {"start": start.isoformat(), "end": end.isoformat(), "power_kw": 6}
    result = compose_forecasts((ForecastVintage(1, start, end, (native,)),), start, end)
    assert result.intervals == ({**native, "source_snapshot_id": 1},)
    assert _energy(result.intervals) == 6
    assert result.prorated is False
    assert result.segments[0] == {
        "start": "2026-10-25T00:00:00+00:00",
        "end": "2026-10-25T01:00:00+00:00",
        "snapshot_id": 1,
    }


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (_at(0).replace(tzinfo=None), _at(1), "timezone-aware"),
        (_at(0), _at(1).replace(tzinfo=None), "timezone-aware"),
        (_at(1), _at(1), "positive duration"),
        (_at(2), _at(1), "positive duration"),
    ],
)
def test_invalid_target_bounds_reject(start: datetime, end: datetime, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        compose_forecasts((), start, end)


@pytest.mark.parametrize(
    "vintage",
    [ForecastVintage(1, _at(0).replace(tzinfo=None), _at(1), ()), _vintage(1, 1, 0)],
)
def test_invalid_native_coverage_rejects_without_falling_back(vintage: ForecastVintage) -> None:
    with pytest.raises(ValueError, match=r"timezone-aware|positive duration"):
        compose_forecasts((vintage, _vintage(2, 0, 2, _row(0, 2, 3))), _at(0), _at(2))


@pytest.mark.parametrize(
    ("vintage", "fault"),
    [
        (ForecastVintage(1, _at(0), _at(1), NULL_ROW), "TypeError"),
        (_vintage(1, 0, 1, {"start": 0, "end": _at(1).isoformat()}), "ValueError"),
        (_vintage(1, 0, 1, {"start": _at(0).isoformat()}), "ValueError"),
        (_vintage(1, 0, 1, {"start": "invalid", "end": _at(1).isoformat()}), "ValueError"),
        (
            _vintage(1, 0, 1, {"start": "2026-11-11T00:00:00", "end": _at(1).isoformat()}),
            "ValueError",
        ),
        (_vintage(1, 0, 1, _row(0.5, 0.5, 1)), "ValueError"),
        (_vintage(1, 0, 1, _row(-0.5, 0.5, 1)), "ValueError"),
        (_vintage(1, 0, 1, _row(0.5, 1.5, 1)), "ValueError"),
        (_vintage(1, 0, 2, _row(0, 1.5, 1), _row(1, 2, 1)), "ValueError"),
    ],
)
def test_a_damaged_native_row_names_its_snapshot_without_falling_back(
    vintage: ForecastVintage, fault: str
) -> None:
    with pytest.raises(DamagedStoredDataError) as damage:
        compose_forecasts((vintage, _vintage(2, 0, 2, _row(0, 2, 3))), _at(0), _at(2))
    assert (damage.value.kind, damage.value.identity, damage.value.fault) == (
        "forecast_snapshot",
        1,
        fault,
    )


def test_priority_is_caller_order_instead_of_snapshot_id() -> None:
    first = _vintage(1, 0, 1, _row(0, 1, 1))
    second = replace(first, snapshot_id=100, intervals=(_row(0, 1, 2),))
    assert compose_forecasts((first, second), _at(0), _at(1)).intervals[0]["power_kw"] == 1
    assert compose_forecasts((second, first), _at(0), _at(1)).intervals[0]["power_kw"] == 2
