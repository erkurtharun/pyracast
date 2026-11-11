from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from pyracast.plants.numerics import CalibrationSample, PowerInterval, fit_calibration

START = datetime(2026, 4, 1, tzinfo=UTC)


def _history(days: int = 150) -> tuple[CalibrationSample, ...]:
    samples = []
    for offset in range(days):
        factor = 1.3 if offset < 80 else 0.8
        for hour in range(10, 13):
            start = START + timedelta(days=offset, hours=hour)
            end = start + timedelta(hours=1)
            samples.append(
                CalibrationSample(start, end, 40 * factor, (PowerInterval(start, end, 40),))
            )
    return tuple(samples)


def _fit(samples: tuple[CalibrationSample, ...]) -> dict[str, object]:
    windows = {
        sample.start.date(): (
            (
                sample.start.replace(hour=10),
                sample.start.replace(hour=13),
            ),
        )
        for sample in samples
    }
    return fit_calibration(
        samples,
        timezone="UTC",
        normalization_mw=100,
        limit_mw=100,
        required_windows=windows,
    ).report


def test_recent_transfer_drift_selects_short_history_before_final_holdout() -> None:
    report = _fit(_history())
    selection = cast(dict[str, object], report["adaptive_window"])
    assert selection["status"] == "selected"
    assert selection["selected_lookback_days"] == 30
    assert 0.8 < cast(float, report["candidate_factor"]) < 0.81
    assert report["training_days"] == 30
    training = cast(list[str], report["training_day_dates"])
    holdout = cast(list[str], report["holdout_day_dates"])
    assert date.fromisoformat(training[0]) == START.date() + timedelta(days=113)
    assert date.fromisoformat(holdout[0]) == START.date() + timedelta(days=143)
    candidate = cast(dict[str, float], report["candidate"])
    assert candidate["interval_mae_mw"] < 0.1


def test_final_holdout_cannot_change_window_selection_or_fit() -> None:
    samples = _history()
    initial = _fit(samples)
    holdout_start = START + timedelta(days=143)
    changed = _fit(
        tuple(
            replace(sample, energy_mwh=sample.energy_mwh * 2)
            if sample.start >= holdout_start
            else sample
            for sample in samples
        )
    )
    assert initial["adaptive_window"] == changed["adaptive_window"]
    assert initial["candidate_factor"] == changed["candidate_factor"]
    assert initial["candidate"] != changed["candidate"]


def test_candidate_folds_share_dates_and_require_full_calendar_history() -> None:
    report = _fit(_history(90))
    selection = cast(dict[str, object], report["adaptive_window"])
    candidates = cast(list[dict[str, object]], selection["candidates"])
    assert [candidate["lookback_days"] for candidate in candidates] == [30, 60, 90]
    assert candidates[2]["status"] == "insufficient_history"
    comparable = [c for c in candidates if c["status"] == "evaluated"]
    dates = []
    for candidate in comparable:
        folds = cast(list[dict[str, object]], candidate["folds"])
        assert len(folds) == 3
        dates.append([fold["evaluation_day_dates"] for fold in folds])
        for fold in folds:
            training = cast(list[str], fold["training_day_dates"])
            evaluation = cast(list[str], fold["evaluation_day_dates"])
            assert max(training) < min(evaluation)
            assert len(training) >= 20
            assert len(evaluation) == 7
            assert max(evaluation) < cast(list[str], report["holdout_day_dates"])[0]
    assert dates[0] == dates[1]


@pytest.mark.parametrize("days", [7, 29, 30, 59])
def test_short_histories_keep_existing_training_split(days: int) -> None:
    report = _fit(_history(days))
    selection = cast(dict[str, object], report["adaptive_window"])
    assert selection["status"] == "insufficient"
    assert selection["selected_lookback_days"] is None
    assert report["training_days"] == days - cast(int, report["holdout_days"])


@pytest.mark.parametrize("sparse", [False, True])
def test_unsupported_inner_folds_skip_a_window_or_keep_all_development_days(sparse: bool) -> None:
    samples = []
    for sample in _history(60):
        offset = (sample.start - START).days
        if sparse:
            shift = timedelta(days=offset)
            sample = replace(
                sample,
                start=sample.start + shift,
                end=sample.end + shift,
                intervals=tuple(
                    replace(piece, start=piece.start + shift, end=piece.end + shift)
                    for piece in sample.intervals
                ),
            )
        elif offset < 40:
            sample = replace(
                sample,
                energy_mwh=0.008,
                intervals=(PowerInterval(sample.start, sample.end, 0.01),),
            )
        samples.append(sample)
    report = _fit(tuple(samples))
    selection = cast(dict[str, object], report["adaptive_window"])
    assert selection["status"] == ("selected" if sparse else "insufficient")
    assert selection["selected_lookback_days"] == (60 if sparse else None)
    candidates = cast(list[dict[str, object]], selection["candidates"])
    assert candidates[0]["status"] == "insufficient_training_support"
    assert report["training_days"] == (30 if sparse else 53)


def test_adaptive_fit_clips_native_power_before_hourly_energy() -> None:
    samples = []
    for sample in _history():
        factor = 1.3 if (sample.start - START).days < 80 else 0.8
        middle = sample.start + timedelta(minutes=30)
        samples.append(
            replace(
                sample,
                energy_mwh=(min(100, 120 * factor) + 20 * factor) / 2,
                intervals=(
                    PowerInterval(sample.start, middle, 120),
                    PowerInterval(middle, sample.end, 20),
                ),
            )
        )
    report = _fit(tuple(samples))
    selection = cast(dict[str, object], report["adaptive_window"])
    assert selection["selected_lookback_days"] == 30
    assert 0.8 < cast(float, report["candidate_factor"]) < 0.81
    candidate = cast(dict[str, float], report["candidate"])
    assert candidate["interval_mae_mw"] < 0.1


def test_lookback_uses_local_calendar_days_across_a_clock_change() -> None:
    zone = ZoneInfo("Europe/Berlin")
    base = datetime(2026, 8, 1, tzinfo=zone)
    samples = []
    windows = {}
    for offset in range(120):
        day = base + timedelta(days=offset)
        windows[day.date()] = ((day.replace(hour=10), day.replace(hour=13)),)
        for hour in range(10, 13):
            start = day.replace(hour=hour)
            end = start + timedelta(hours=1)
            samples.append(CalibrationSample(start, end, 32, (PowerInterval(start, end, 40),)))
    report = fit_calibration(
        tuple(samples),
        timezone=str(zone),
        normalization_mw=100,
        limit_mw=100,
        required_windows=windows,
    ).report
    selection = cast(dict[str, object], report["adaptive_window"])
    assert selection["selected_lookback_days"] == 30
    training = cast(list[str], report["training_day_dates"])
    holdout = cast(list[str], report["holdout_day_dates"])
    assert date.fromisoformat(holdout[0]) - date.fromisoformat(training[0]) == timedelta(days=30)
    first = datetime.combine(date.fromisoformat(training[0]), datetime.min.time(), zone)
    stop = datetime.combine(date.fromisoformat(holdout[0]), datetime.min.time(), zone)
    assert stop.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=721)
