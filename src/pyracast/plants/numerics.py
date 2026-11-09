from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from math import ceil, isfinite
from zoneinfo import ZoneInfo

MODEL_VERSION = "reference-power-transfer-v1"
POLICY_VERSION = "regularized-huber-v1"
FACTOR_MIN = 0.25
FACTOR_MAX = 2.0
HUBER_DELTA = 0.05
IDENTITY_PENALTY = 0.001
CAP_MARGIN = 0.98
MIN_IMPROVEMENT = 0.01


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
    normalized.sort(key=lambda interval: interval.start)
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
    return {
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


def _rows(
    samples: tuple[CalibrationSample, ...],
    zone: ZoneInfo,
) -> tuple[list[_Row], dict[str, int], list[date]]:
    rows: list[_Row] = []
    exclusions = {"quality": 0, "source_incomplete": 0, "incomplete_day": 0}
    previous_end: datetime | None = None
    for sample in sorted(samples, key=lambda sample: _bounds(sample.start, sample.end)[0]):
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
        hours = (day_end.astimezone(UTC) - day_start.astimezone(UTC)).total_seconds() / 3600
        day_rows = [row for row in rows if row.day == day]
        if abs(sum(row.hours for row in day_rows) - hours) < 1e-9 and any(
            row.sample.energy_mwh > 0 and any(p > 0 for p, _ in row.pieces) for row in day_rows
        ):
            valid.append(day)
        else:
            exclusions["incomplete_day"] += len(day_rows)
    return [row for row in rows if row.day in valid], exclusions, valid


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

    candidates = list(boundaries)
    for left, right in pairwise(boundaries):
        midpoint = (left + right) / 2
        slopes = [
            sum(
                power * duration
                for power, duration in row.pieces
                if cap is None or midpoint * power < cap
            )
            / row.hours
            for row in rows
        ]

        def gradient(factor: float, slopes: list[float] = slopes) -> float:
            result = 2 * IDENTITY_PENALTY * (factor - 1)
            for row, slope in zip(rows, slopes, strict=True):
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

        if gradient(left) < 0 < gradient(right):
            for _ in range(60):
                midpoint = (left + right) / 2
                if gradient(midpoint) < 0:
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
    return {name: _finite(value, "Calibration metric") for name, value in metrics.items()}


def fit_calibration(
    samples: tuple[CalibrationSample, ...],
    *,
    timezone: str,
    normalization_mw: float,
    limit_mw: float | None,
) -> FitResult:
    _positive(normalization_mw, "normalization_mw")
    if limit_mw is not None:
        _positive(limit_mw, "limit_mw")
    rows, exclusions, days = _rows(samples, ZoneInfo(timezone))
    report: dict[str, object] = {
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
    }
    if len(days) < 7:
        return FitResult("insufficient", None, "insufficient", report)
    holdout_count = 7 if len(days) >= 30 else max(2, ceil(len(days) * 0.2))
    holdout_days = days[-holdout_count:]
    training = [row for row in rows if row.day not in holdout_days]
    validation = [row for row in rows if row.day in holdout_days]
    informative = [row for row in training if any(power > 0 for power, _ in row.pieces)]
    eligible = [
        row
        for row in informative
        if limit_mw is None or row.sample.energy_mwh / row.hours < CAP_MARGIN * limit_mw
    ]
    exclusions["uninformative_training"] = len(training) - len(informative)
    exclusions["censored_training"] = len(informative) - len(eligible)
    report.update(
        {
            "training_days": len(days) - holdout_count,
            "holdout_days": holdout_count,
            "fit_samples": len(eligible),
            "holdout_start": holdout_days[0].isoformat(),
            "training_day_dates": [day.isoformat() for day in days[:-holdout_count]],
            "holdout_day_dates": [day.isoformat() for day in holdout_days],
        }
    )
    if not eligible:
        return FitResult("insufficient", None, "insufficient", report)
    powers = [power for row in eligible for power, _ in row.pieces if power > 0]
    report["power_regime_mw"] = {"min": min(powers), "max": max(powers)}
    factor = _fit(eligible, normalization_mw, limit_mw)
    baseline = _metrics(validation, 1, limit_mw)
    candidate = _metrics(validation, factor, limit_mw)
    report.update({"candidate_factor": factor, "baseline": baseline, "candidate": candidate})
    improves = all(
        candidate[metric] < baseline[metric] * (1 - MIN_IMPROVEMENT)
        for metric in ("interval_mae_mw", "daily_mae_mwh")
    )
    accepted = improves and FACTOR_MIN + 1e-6 < factor < FACTOR_MAX - 1e-6
    return FitResult(
        "ready" if accepted else "rejected",
        factor if accepted else None,
        "evaluated" if len(days) >= 30 else "provisional",
        report,
    )
