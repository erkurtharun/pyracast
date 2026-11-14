from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from math import ceil, isfinite
from statistics import median
from typing import cast
from zoneinfo import ZoneInfo

from pyracast.solar import Daylight, TimeWindows, daylight_energy_windows, summarize_daylight

MODEL_VERSION = "reference-power-transfer-v1"
POLICY_VERSION = "regularized-huber-v7"
DAYLIGHT_COVERAGE_POLICY = "whole-meter-intervals-cover-daylight-v1"
type RequiredWindows = Mapping[date, tuple[tuple[datetime, datetime], ...]]
FACTOR_MIN = 0.25
FACTOR_MAX = 2.0
HUBER_DELTA = 0.05
IDENTITY_PENALTY = 0.001
CAP_MARGIN = 0.98
MIN_IMPROVEMENT = 0.01
MIN_SUPPORT_DAYS = 5
MIN_SUPPORT_HOURS = 2.0
MIN_LEVERAGE_HOURS = 0.02
COMPARISON_TOLERANCE = 1e-9
TRAINING_WINDOW_POLICY = "recent-calendar-days-v1"
TRAINING_LOOKBACK_DAYS = 30
DERATING_POLICY = "strong-unclipped-power-diagnostics-v2"
DERATING_LOOKBACK_DAYS = 10
DERATING_MIN_DAYS = 3
DERATING_MIN_HOURS = 3.0
DERATING_MIN_POWER_FRACTION = 0.25
DERATING_THRESHOLD = 0.92


@dataclass(frozen=True)
class PowerInterval:
    start: datetime
    end: datetime
    power_mw: float | None


@dataclass(frozen=True)
class CalibrationSample:
    start: datetime
    end: datetime
    energy_mwh: float
    intervals: tuple[PowerInterval, ...]
    quality: str = "valid"


@dataclass(frozen=True)
class FitResult:
    status: str
    factor: float | None
    assessment: str
    report: dict[str, object]


@dataclass(frozen=True)
class _Row:
    day: date
    sample: CalibrationSample
    pieces: tuple[tuple[float, float], ...]
    hours: float


def _bounds(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("Interval bounds must be timezone-aware")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end <= start:
        raise ValueError("Interval end must follow start")
    return start, end


def _positive(value: float, name: str) -> None:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def _finite(value: float, name: str) -> float:
    if not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _validate(intervals: tuple[PowerInterval, ...]) -> tuple[PowerInterval, ...]:
    normalized = []
    for interval in intervals:
        start, end = _bounds(interval.start, interval.end)
        power = interval.power_mw
        if power is not None and (not isfinite(power) or power < 0):
            raise ValueError("Power must be finite and nonnegative, or missing")
        normalized.append(PowerInterval(start, end, power))
    normalized.sort(key=lambda item: item.start)
    if any(a.end > b.start for a, b in pairwise(normalized)):
        raise ValueError("Power intervals must not overlap")
    return tuple(normalized)


def _pieces(
    intervals: tuple[PowerInterval, ...], start: datetime, end: datetime
) -> tuple[tuple[float, float], ...]:
    return tuple(
        (
            interval.power_mw,
            (min(end, interval.end) - max(start, interval.start)).total_seconds() / 3600,
        )
        for interval in intervals
        if interval.power_mw is not None and interval.start < end and interval.end > start
    )


def _energy(pieces: tuple[tuple[float, float], ...], factor: float, cap: float | None) -> float:
    energy = sum(
        (power * factor if cap is None else min(cap, power * factor)) * hours
        for power, hours in pieces
    )
    if not isfinite(energy):
        raise ValueError("Calculated energy must be finite")
    return energy


def forecast(
    intervals: tuple[PowerInterval, ...],
    *,
    start: datetime,
    end: datetime,
    resolution_minutes: int,
    limit_mw: float | None,
    factor: float = 1.0,
    daylight: Daylight | None = None,
    energy_windows: TimeWindows | None = None,
) -> dict[str, object]:
    start, end = _bounds(start, end)
    if resolution_minutes not in (30, 60):
        raise ValueError("Resolution must be 30 or 60 minutes")
    _positive(factor, "factor")
    if limit_mw is not None:
        _positive(limit_mw, "limit_mw")
    intervals = _validate(intervals)
    records: list[dict[str, object]] = []
    prorated = False
    cursor = start
    partial = covered_hours = 0.0
    while cursor < end:
        stop = min(end, cursor + timedelta(minutes=resolution_minutes))
        pieces = _pieces(intervals, cursor, stop)
        prorated = prorated or any(
            interval.power_mw is not None
            and interval.start < stop
            and interval.end > cursor
            and (interval.start < cursor or interval.end > stop)
            for interval in intervals
        )
        hours = (stop - cursor).total_seconds() / 3600
        coverage = sum(duration for _, duration in pieces)
        energy = _energy(pieces, factor, limit_mw)
        complete = abs(coverage - hours) < 1e-9
        records.append(
            {
                "start": cursor.isoformat(),
                "end": stop.isoformat(),
                "mean_power_mw": energy / hours if complete else None,
                "energy_mwh": energy if complete else None,
                "partial_energy_mwh": energy,
                "coverage_fraction": coverage / hours,
                "complete": complete,
            }
        )
        partial += energy
        covered_hours += coverage
        cursor = stop
    hours = (end - start).total_seconds() / 3600
    complete = abs(covered_hours - hours) < 1e-9
    _finite(partial, "Forecast total energy")
    result: dict[str, object] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "resolution_minutes": resolution_minutes,
        "limit_mw": limit_mw,
        "factor": factor,
        "energy_mwh": partial if complete else None,
        "partial_energy_mwh": partial,
        "coverage_fraction": covered_hours / hours,
        "complete": complete,
        "intervals": records,
        "model_version": MODEL_VERSION,
        "quality_flags": ["unvalidated_plant_proxy"]
        + ([] if complete else ["incomplete"])
        + (["uniform_mean_proration"] if prorated else []),
    }
    if daylight is not None:
        result["daylight"] = summarize_daylight(
            daylight,
            (
                (
                    interval.start,
                    interval.end,
                    None
                    if interval.power_mw is None
                    else interval.power_mw * factor
                    if limit_mw is None
                    else min(limit_mw, interval.power_mw * factor),
                )
                for interval in intervals
            ),
            start=start,
            end=end,
            energy_windows=energy_windows
            if energy_windows is not None
            else daylight_energy_windows(
                daylight, ((interval.start, interval.end) for interval in intervals)
            ),
            unit="mwh",
        )
    return result


def _rows(
    samples: tuple[CalibrationSample, ...],
    zone: ZoneInfo,
    *,
    required_windows: RequiredWindows | None = None,
) -> tuple[list[_Row], dict[str, int], list[date]]:
    rows: list[_Row] = []
    exclusions = {"quality": 0, "source_incomplete": 0, "incomplete_day": 0}
    windows_by_day = _required_windows(required_windows, zone)
    if windows_by_day is not None:
        exclusions["outside_required_windows"] = 0
    previous_end: datetime | None = None
    for sample in sorted(samples, key=lambda item: _bounds(item.start, item.end)[0]):
        start, end = _bounds(sample.start, sample.end)
        if previous_end is not None and start < previous_end:
            raise ValueError("Meter intervals must not overlap")
        previous_end = end
        if not isfinite(sample.energy_mwh) or sample.energy_mwh < 0:
            raise ValueError("Meter energy must be finite and nonnegative")
        day = start.astimezone(zone).date()
        day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone)
        if end > day_end.astimezone(UTC):
            raise ValueError("Meter intervals must not cross local midnight")
        intervals = _validate(sample.intervals)
        pieces = _pieces(intervals, start, end)
        hours = (end - start).total_seconds() / 3600
        if windows_by_day is not None and not any(
            start < stop and end > begin for begin, stop in windows_by_day.get(day, ())
        ):
            exclusions["outside_required_windows"] += 1
            continue
        if sample.quality != "valid":
            exclusions["quality"] += 1
        elif abs(sum(duration for _, duration in pieces) - hours) > 1e-9:
            exclusions["source_incomplete"] += 1
        else:
            rows.append(_Row(day, sample, pieces, hours))
    days = sorted({row.day for row in rows})
    valid = []
    for day in days:
        day_start = datetime.combine(day, datetime.min.time(), zone)
        day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone)
        day_rows = [row for row in rows if row.day == day]
        windows = (
            ((day_start.astimezone(UTC), day_end.astimezone(UTC)),)
            if windows_by_day is None
            else windows_by_day.get(day, ())
        )
        if windows and all(_covers_window(day_rows, start, end) for start, end in windows):
            valid.append(day)
        else:
            exclusions["incomplete_day"] += len(day_rows)
    return [row for row in rows if row.day in valid], exclusions, valid


def _required_windows(
    windows: RequiredWindows | None, zone: ZoneInfo
) -> dict[date, tuple[tuple[datetime, datetime], ...]] | None:
    if windows is None:
        return None
    normalized = {}
    for day, intervals in windows.items():
        bounds = tuple(sorted(_bounds(start, end) for start, end in intervals))
        day_start = datetime.combine(day, datetime.min.time(), zone).astimezone(UTC)
        day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone).astimezone(
            UTC
        )
        if any(start < day_start or end > day_end for start, end in bounds):
            raise ValueError("Required windows must stay within their local day")
        if any(left[1] > right[0] for left, right in pairwise(bounds)):
            raise ValueError("Required windows must not overlap")
        normalized[day] = bounds
    return normalized


def _covers_window(rows: list[_Row], start: datetime, end: datetime) -> bool:
    cursor = start
    for row in rows:
        begin, stop = _bounds(row.sample.start, row.sample.end)
        if stop <= cursor or begin >= end:
            continue
        if begin > cursor:
            return False
        cursor = max(cursor, stop)
        if cursor >= end:
            return True
    return False


def _scope(required_windows: RequiredWindows | None) -> dict[str, object]:
    return {
        "metric_scope": "full_day" if required_windows is None else "daylight_intervals",
        "coverage_policy": "full-local-day-v1"
        if required_windows is None
        else DAYLIGHT_COVERAGE_POLICY,
    }


def _huber(residual: float) -> float:
    absolute = abs(residual)
    return (
        0.5 * residual * residual
        if absolute <= HUBER_DELTA
        else HUBER_DELTA * (absolute - 0.5 * HUBER_DELTA)
    )


def _fit(rows: list[_Row], normalization: float, cap: float | None) -> float:
    hours = sum(row.hours for row in rows)
    points = {FACTOR_MIN, FACTOR_MAX}
    if cap is not None:
        points.update(
            cap / power
            for row in rows
            for power, _ in row.pieces
            if power > 0 and FACTOR_MIN < cap / power < FACTOR_MAX
        )
    boundaries = sorted(points)

    def loss(factor: float) -> float:
        return _finite(
            sum(
                row.hours
                / hours
                * _huber(
                    ((row.sample.energy_mwh - _energy(row.pieces, factor, cap)) / row.hours)
                    / normalization
                )
                for row in rows
            )
            + IDENTITY_PENALTY * (factor - 1) ** 2,
            "Calibration loss",
        )

    def gradient(factor: float, interval_slopes: Sequence[float]) -> float:
        result = 2 * IDENTITY_PENALTY * (factor - 1)
        for row, slope in zip(rows, interval_slopes, strict=True):
            residual = (
                (row.sample.energy_mwh - _energy(row.pieces, factor, cap)) / row.hours
            ) / normalization
            result -= (
                row.hours
                / hours
                * max(-HUBER_DELTA, min(HUBER_DELTA, residual))
                * (slope / normalization)
            )
        return _finite(result, "Calibration gradient")

    candidates = list(boundaries)
    for left, right in pairwise(boundaries):
        midpoint = (left + right) / 2
        segment_slopes = [
            sum(
                power * duration
                for power, duration in row.pieces
                if cap is None or midpoint * power < cap
            )
            / row.hours
            for row in rows
        ]
        if gradient(left, segment_slopes) < 0 < gradient(right, segment_slopes):
            for _ in range(60):
                midpoint = (left + right) / 2
                if gradient(midpoint, segment_slopes) < 0:
                    left = midpoint
                else:
                    right = midpoint
            candidates.append((left + right) / 2)
    return min(candidates, key=loss)


def _metrics(rows: list[_Row], factor: float, cap: float | None) -> dict[str, float]:
    errors = [(row, _energy(row.pieces, factor, cap) - row.sample.energy_mwh) for row in rows]
    hours = sum(row.hours for row in rows)
    daily: dict[date, float] = {}
    for row, error in errors:
        daily[row.day] = daily.get(row.day, 0.0) + error
    metrics = {
        "interval_mae_mw": sum(abs(error) for _, error in errors) / hours,
        "bias_mw": sum(error for _, error in errors) / hours,
        "daily_mae_mwh": sum(abs(error) for error in daily.values()) / len(daily),
        "daily_bias_mwh": sum(daily.values()) / len(daily),
    }
    source_positive_errors = [
        (row, error) for row, error in errors if any(power > 0 for power, _ in row.pieces)
    ]
    source_positive_hours = sum(row.hours for row, _ in source_positive_errors)
    if source_positive_hours:
        metrics["source_positive_mae_mw"] = (
            sum(abs(error) for _, error in source_positive_errors) / source_positive_hours
        )
    return {name: _finite(value, "Calibration metric") for name, value in metrics.items()}


def evaluate_samples(
    samples: tuple[CalibrationSample, ...],
    *,
    timezone: str,
    factor: float = 1.0,
    limit_mw: float | None,
    required_windows: RequiredWindows | None = None,
) -> dict[str, object]:
    _positive(factor, "factor")
    if limit_mw is not None:
        _positive(limit_mw, "limit_mw")
    rows, exclusions, days = _rows(
        samples,
        ZoneInfo(timezone),
        required_windows=required_windows,
    )
    daily = []
    for day in days:
        day_rows = [row for row in rows if row.day == day]
        measured = _finite(sum(row.sample.energy_mwh for row in day_rows), "Measured daily energy")
        predicted = _finite(
            sum(_energy(row.pieces, factor, limit_mw) for row in day_rows),
            "Predicted daily energy",
        )
        daily.append(
            {
                "day": day.isoformat(),
                "measured_energy_mwh": measured,
                "predicted_energy_mwh": predicted,
                "error_mwh": _finite(predicted - measured, "Daily energy error"),
            }
        )
    return {
        **_scope(required_windows),
        "valid_days": len(days),
        "samples": len(rows),
        "exclusions": exclusions,
        "included_day_dates": [day.isoformat() for day in days],
        "metrics": {"source_positive_mae_mw": None, **_metrics(rows, factor, limit_mw)}
        if rows
        else None,
        "day_errors": daily,
        "factor": factor,
        "limit_mw": limit_mw,
    }


def _eligible(rows: list[_Row], cap: float | None) -> list[_Row]:
    informative_days = {
        row.day
        for row in rows
        if row.sample.energy_mwh > 0 and any(power > 0 for power, _ in row.pieces)
    }
    return [
        row
        for row in rows
        if row.day in informative_days
        and any(power > 0 for power, _ in row.pieces)
        and (cap is None or row.sample.energy_mwh / row.hours < CAP_MARGIN * cap)
    ]


def _support(
    rows: list[_Row], days: list[date], normalization: float, cap: float | None
) -> dict[str, object]:
    daily: list[dict[str, object]] = []
    supported = 0
    for day in days:
        hours = leverage = 0.0
        for row in rows:
            if row.day != day:
                continue
            pieces = tuple(
                (power, duration)
                for power, duration in row.pieces
                if power > 0 and (cap is None or FACTOR_MIN * power < cap)
            )
            hours += sum(duration for _, duration in pieces)
            slope = sum(power * duration for power, duration in pieces) / row.hours
            leverage += row.hours * (min(slope, normalization) / normalization) ** 2
        sufficient = hours >= MIN_SUPPORT_HOURS and leverage >= MIN_LEVERAGE_HOURS
        supported += sufficient
        daily.append(
            {
                "day": day.isoformat(),
                "informative_hours": hours,
                "normalized_leverage_hours": leverage,
                "sufficient": sufficient,
            }
        )
    return {
        "minimum_days": MIN_SUPPORT_DAYS,
        "minimum_hours_per_day": MIN_SUPPORT_HOURS,
        "minimum_leverage_hours_per_day": MIN_LEVERAGE_HOURS,
        "informative_days": supported,
        "day_support": daily,
        "sufficient": supported >= MIN_SUPPORT_DAYS,
    }


def _derating(
    rows: list[_Row],
    end: date | None,
    baseline: float,
    normalization: float,
    cap: float | None,
    reference_kind: str,
) -> dict[str, object]:
    first = end - timedelta(days=DERATING_LOOKBACK_DAYS - 1) if end is not None else None
    totals: dict[date, list[float]] = {}
    limit_reached = False
    for row in rows:
        if first is not None and row.day < first:
            continue
        predicted = _energy(row.pieces, baseline, cap)
        ceiling = None if cap is None else CAP_MARGIN * cap * row.hours
        if ceiling is not None and row.sample.energy_mwh >= ceiling:
            limit_reached = True
        if predicted < DERATING_MIN_POWER_FRACTION * normalization * row.hours or (
            ceiling is not None and max(predicted, row.sample.energy_mwh) >= ceiling
        ):
            continue
        day = totals.setdefault(row.day, [0.0, 0.0, 0.0])
        day[0] += row.sample.energy_mwh
        day[1] += predicted
        day[2] += row.hours
    daily = [
        {
            "day": day.isoformat(),
            "performance_index": _finite(measured / predicted, "Performance index"),
            "hours": hours,
            "status": "shortfall"
            if measured / predicted < DERATING_THRESHOLD
            else "within_threshold",
            "observed_zero": not any(row.sample.energy_mwh > 0 for row in rows if row.day == day),
        }
        for day, (measured, predicted, hours) in sorted(totals.items())
        if hours >= DERATING_MIN_HOURS
    ]
    ratio = (
        median(cast(float, day["performance_index"]) for day in daily)
        if len(daily) >= DERATING_MIN_DAYS
        else None
    )
    latest = next((day for day in daily if day["day"] == str(end)), None)
    return {
        "policy": DERATING_POLICY,
        "status": "insufficient"
        if ratio is None
        else "suspected"
        if ratio < DERATING_THRESHOLD
        else "normal",
        "baseline_factor": baseline,
        "reference_kind": reference_kind,
        "performance_ratio": ratio,
        "threshold": DERATING_THRESHOLD,
        "window_start": first.isoformat() if first is not None else None,
        "window_end": end.isoformat() if end is not None else None,
        "latest_matched_day": end.isoformat() if end is not None else None,
        "latest_day_status": latest["status"]
        if latest is not None
        else "insufficient"
        if end is not None
        else "unavailable",
        "latest_performance_index": latest["performance_index"] if latest is not None else None,
        "days": daily,
        "limit_reached": limit_reached,
    }


def _worse(candidate: float, reference: float) -> bool:
    return candidate > reference + COMPARISON_TOLERANCE * max(1, abs(reference))


def _meaningful_improvement(candidate: dict[str, float], reference: dict[str, float]) -> bool:
    return all(
        candidate[metric]
        < reference[metric] - COMPARISON_TOLERANCE * max(1, abs(reference[metric]))
        and candidate[metric] <= reference[metric] * (1 - MIN_IMPROVEMENT)
        for metric in ("interval_mae_mw", "daily_mae_mwh")
    ) and all(
        not _worse(abs(candidate[metric]), abs(reference[metric]))
        for metric in ("bias_mw", "daily_bias_mwh")
    )


def _rolling_origin(
    rows: list[_Row], days: list[date], normalization: float, cap: float | None, incumbent: float
) -> dict[str, object]:
    folds: list[dict[str, object]] = []
    for offset in (9, 6, 3):
        evaluation_start = len(days) - offset
        training_days = days[:evaluation_start]
        evaluation_days = days[evaluation_start : evaluation_start + 3]
        eligible = _eligible([row for row in rows if row.day in training_days], cap)
        if not _support(eligible, training_days, normalization, cap)["sufficient"]:
            continue
        factor = _fit(eligible, normalization, cap)
        validation = [row for row in rows if row.day in evaluation_days]
        folds.append(
            {
                "training_day_dates": [day.isoformat() for day in training_days],
                "evaluation_day_dates": [day.isoformat() for day in evaluation_days],
                "factor": factor,
                "baseline": _metrics(validation, 1, cap),
                "candidate": _metrics(validation, factor, cap),
                "incumbent": _metrics(validation, incumbent, cap),
            }
        )
    return {"status": "diagnostic" if folds else "insufficient", "folds": folds}


def fit_calibration(
    samples: tuple[CalibrationSample, ...],
    *,
    timezone: str,
    normalization_mw: float,
    limit_mw: float | None,
    incumbent_factor: float = 1.0,
    incumbent_is_active: bool | None = None,
    required_windows: RequiredWindows | None = None,
) -> FitResult:
    _positive(normalization_mw, "normalization_mw")
    if limit_mw is not None:
        _positive(limit_mw, "limit_mw")
    _positive(incumbent_factor, "incumbent_factor")
    if not FACTOR_MIN <= incumbent_factor <= FACTOR_MAX:
        raise ValueError("incumbent_factor must be inside the allowed factor bounds")
    rows, exclusions, days = _rows(samples, ZoneInfo(timezone), required_windows=required_windows)
    report: dict[str, object] = {
        **_scope(required_windows),
        "policy_version": POLICY_VERSION,
        "valid_days": len(days),
        "samples": len(rows),
        "exclusions": exclusions,
        "uncertainty": "not_estimated",
        "window_start": days[0].isoformat() if days else None,
        "window_end": days[-1].isoformat() if days else None,
        "included_day_dates": [day.isoformat() for day in days],
        "training_day_dates": [],
        "holdout_day_dates": [],
        "normalization_mw": normalization_mw,
        "limit_mw": limit_mw,
        "factor_bounds": [FACTOR_MIN, FACTOR_MAX],
        "huber_delta": HUBER_DELTA,
        "identity_penalty": IDENTITY_PENALTY,
        "cap_margin": CAP_MARGIN,
        "incumbent_factor": incumbent_factor,
        "reasons": [],
        "rolling_origin": {"status": "insufficient", "folds": []},
        "candidate_kind": None,
        "derating": _derating(
            rows,
            days[-1] if days else None,
            incumbent_factor,
            normalization_mw,
            limit_mw,
            "active_calibration"
            if (incumbent_factor != 1 if incumbent_is_active is None else incumbent_is_active)
            else "uncalibrated_reference",
        ),
        "training_window": {
            "policy": TRAINING_WINDOW_POLICY,
            "lookback_days": TRAINING_LOOKBACK_DAYS,
        },
    }
    if len(days) < 7:
        report["reasons"] = ["insufficient_complete_days"]
        return FitResult("insufficient", None, "insufficient", report)
    holdout_count = 7 if len(days) >= 30 else max(2, ceil(len(days) * 0.2))
    holdout_days = days[-holdout_count:]
    first_training_day = holdout_days[0] - timedelta(days=TRAINING_LOOKBACK_DAYS)
    training_days = [day for day in days[:-holdout_count] if day >= first_training_day]
    training = [row for row in rows if first_training_day <= row.day < holdout_days[0]]
    validation = [row for row in rows if row.day in holdout_days]
    informative = _eligible(training, None)
    eligible = _eligible(informative, limit_mw)
    exclusions["uninformative_training"] = len(training) - len(informative)
    exclusions["zero_production_training_days"] = sum(
        not any(row.sample.energy_mwh > 0 for row in training if row.day == day)
        for day in training_days
    )
    exclusions["censored_training"] = len(informative) - len(eligible)
    support = _support(eligible, training_days, normalization_mw, limit_mw)
    report.update(
        {
            "training_days": len(training_days),
            "holdout_days": holdout_count,
            "fit_samples": len(eligible),
            "holdout_start": holdout_days[0].isoformat(),
            "training_day_dates": [day.isoformat() for day in training_days],
            "holdout_day_dates": [day.isoformat() for day in holdout_days],
            "training_support": support,
        }
    )
    if not support["sufficient"]:
        report["reasons"] = ["insufficient_training_support"]
        return FitResult("insufficient", None, "insufficient", report)
    powers = [power for row in eligible for power, _ in row.pieces if power > 0]
    report["power_regime_mw"] = {"min": min(powers), "max": max(powers)}
    factor = _fit(eligible, normalization_mw, limit_mw)
    baseline = _metrics(validation, 1, limit_mw)
    candidate = _metrics(validation, factor, limit_mw)
    incumbent = _metrics(validation, incumbent_factor, limit_mw)
    report.update(
        {
            "candidate_factor": factor,
            "candidate_kind": "huber",
            "fitted_factor": factor,
            "fitted_metrics": candidate,
            "baseline": baseline,
            "candidate": candidate,
            "incumbent": incumbent,
        }
    )
    if len(days) >= 30:
        report["rolling_origin"] = _rolling_origin(
            training, training_days, normalization_mw, limit_mw, incumbent_factor
        )
    improves = all(
        candidate[metric] < baseline[metric] * (1 - MIN_IMPROVEMENT)
        for metric in ("interval_mae_mw", "daily_mae_mwh")
    )
    reasons = []
    if not improves:
        reasons.append("baseline_improvement_below_margin")
    if not FACTOR_MIN + 1e-6 < factor < FACTOR_MAX - 1e-6:
        reasons.append("factor_at_bound")
    if any(
        _worse(candidate[metric], incumbent[metric])
        for metric in ("interval_mae_mw", "daily_mae_mwh")
    ):
        reasons.append("incumbent_mae_regression")
    for name, reference in (("baseline", baseline), ("incumbent", incumbent)):
        if any(
            _worse(abs(candidate[metric]), abs(reference[metric]))
            for metric in ("bias_mw", "daily_bias_mwh")
        ):
            reasons.append(f"{name}_bias_regression")
    if reasons and incumbent_factor != 1 and _meaningful_improvement(baseline, incumbent):
        factor, candidate, reasons = 1.0, baseline, []
        report.update(
            {
                "candidate_kind": "identity_recovery",
                "candidate_factor": factor,
                "candidate": candidate,
            }
        )
    report["reasons"] = reasons
    accepted = not reasons
    return FitResult(
        "ready" if accepted else "rejected",
        factor if accepted else None,
        "evaluated" if len(days) >= 30 else "provisional",
        report,
    )
