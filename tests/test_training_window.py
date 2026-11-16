from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from pyracast.plants.numerics import CalibrationSample, PowerInterval, fit_calibration

START = datetime(2026, 4, 1, tzinfo=UTC)
WINDOW = {"policy": "recent-calendar-days-v1", "lookback_days": 30}


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


def test_fit_uses_the_thirty_calendar_days_before_the_final_holdout() -> None:
    report = _fit(_history())
    assert report["training_window"] == WINDOW
    assert 0.8 < cast(float, report["candidate_factor"]) < 0.81
    assert report["training_days"] == 30
    training = cast(list[str], report["training_day_dates"])
    holdout = cast(list[str], report["holdout_day_dates"])
    assert date.fromisoformat(training[0]) == START.date() + timedelta(days=113)
    assert date.fromisoformat(holdout[0]) == START.date() + timedelta(days=143)
    candidate = cast(dict[str, float], report["candidate"])
    assert candidate["interval_mae_mw"] < 0.1


def test_neither_the_holdout_nor_older_days_can_change_the_fit() -> None:
    samples = _history()
    initial = _fit(samples)
    training_start = START + timedelta(days=113)
    holdout_start = START + timedelta(days=143)
    changed = _fit(
        tuple(
            sample
            if training_start <= sample.start < holdout_start
            else replace(sample, energy_mwh=sample.energy_mwh * 2)
            for sample in samples
        )
    )
    assert initial["candidate_factor"] == changed["candidate_factor"]
    assert initial["training_day_dates"] == changed["training_day_dates"]
    assert initial["candidate"] != changed["candidate"]


@pytest.mark.parametrize(("days", "training"), [(7, 5), (29, 23), (30, 23), (59, 30)])
def test_short_histories_train_on_every_recent_day_before_the_holdout(
    days: int, training: int
) -> None:
    report = _fit(_history(days))
    assert report["training_window"] == WINDOW
    assert report["training_days"] == training
    assert report["training_days"] == min(30, days - cast(int, report["holdout_days"]))


def test_sparse_history_keeps_only_days_inside_the_calendar_window() -> None:
    samples = []
    for sample in _history(60):
        shift = timedelta(days=(sample.start - START).days)
        samples.append(
            replace(
                sample,
                start=sample.start + shift,
                end=sample.end + shift,
                intervals=tuple(
                    replace(piece, start=piece.start + shift, end=piece.end + shift)
                    for piece in sample.intervals
                ),
            )
        )
    report = _fit(tuple(samples))
    assert report["valid_days"] == 60
    assert report["training_days"] == 15
    training = cast(list[str], report["training_day_dates"])
    holdout = cast(list[str], report["holdout_day_dates"])
    assert date.fromisoformat(holdout[0]) - date.fromisoformat(training[0]) == timedelta(days=30)


@pytest.mark.parametrize(("training_days", "fold_count"), [(7, 0), (8, 1), (9, 1), (10, 1)])
def test_sparse_recent_training_does_not_create_wrapped_rolling_folds(
    training_days: int, fold_count: int
) -> None:
    offsets = set(range(23 - training_days)) | set(range(53 - training_days, 60))
    report = _fit(
        tuple(sample for sample in _history(60) if (sample.start - START).days in offsets)
    )
    assert report["valid_days"] == 30
    assert report["training_days"] == training_days
    rolling = cast(dict[str, object], report["rolling_origin"])
    assert rolling["status"] == ("diagnostic" if fold_count else "insufficient")
    folds = cast(list[dict[str, object]], rolling["folds"])
    assert len(folds) == fold_count
    holdout = cast(list[str], report["holdout_day_dates"])
    for fold in folds:
        training = cast(list[str], fold["training_day_dates"])
        evaluation = cast(list[str], fold["evaluation_day_dates"])
        assert len(evaluation) == 3
        assert max(training) < min(evaluation)
        assert max(evaluation) < min(holdout)


def test_fit_clips_native_power_before_hourly_energy() -> None:
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
    training = cast(list[str], report["training_day_dates"])
    holdout = cast(list[str], report["holdout_day_dates"])
    assert date.fromisoformat(holdout[0]) - date.fromisoformat(training[0]) == timedelta(days=30)
    first = datetime.combine(date.fromisoformat(training[0]), datetime.min.time(), zone)
    stop = datetime.combine(date.fromisoformat(holdout[0]), datetime.min.time(), zone)
    assert stop.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=721)
