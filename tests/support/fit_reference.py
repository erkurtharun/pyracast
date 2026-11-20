import hashlib
import json
import math
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

from pyracast.plants import numerics
from pyracast.plants.numerics import (
    FACTOR_MAX,
    FACTOR_MIN,
    HUBER_DELTA,
    IDENTITY_PENALTY,
    CalibrationSample,
    FitResult,
    PowerInterval,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def exhaustive_fit(rows: list[numerics._Row], normalization: float, cap: float | None) -> float:
    """The fit evaluated on every segment and every boundary; the oracle of `numerics._fit`."""
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
        return numerics._finite(
            sum(
                row.hours
                / hours
                * numerics._huber(
                    (
                        (row.sample.energy_mwh - numerics._energy(row.pieces, factor, cap))
                        / row.hours
                    )
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
                (row.sample.energy_mwh - numerics._energy(row.pieces, factor, cap)) / row.hours
            ) / normalization
            result -= (
                row.hours
                / hours
                * max(-HUBER_DELTA, min(HUBER_DELTA, residual))
                * (slope / normalization)
            )
        return numerics._finite(result, "Calibration gradient")

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


class _Sequence:
    """A seeded linear congruential sequence: deterministic fixtures without `random`."""

    def __init__(self, seed: int) -> None:
        self.state = seed + 1

    def unit(self) -> float:
        self.state = (self.state * 6364136223846793005 + 1442695040888963407) % 2**64
        return (self.state >> 11) / 2**53

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * self.unit()

    def choice[T](self, values: tuple[T, ...]) -> T:
        return values[int(self.unit() * len(values))]

    def noise(self) -> float:
        return sum(self.unit() for _ in range(12)) - 6


def random_rows(seed: int, count: int) -> tuple[list[numerics._Row], float, float]:
    sequence = _Sequence(seed)
    cap = sequence.choice((50.0, 80.0, 100.0))
    factor = sequence.uniform(0.3, 1.8)
    rows = []
    for index in range(count):
        start = START + timedelta(minutes=30 * index)
        pieces = []
        for _ in range(sequence.choice((1, 2, 3))):
            kind = sequence.unit()
            power = (
                0.0
                if kind < 0.1
                else float(sequence.choice(tuple(range(1, 13))) * 10)
                if kind < 0.3
                else sequence.uniform(0, 4 * cap)
            )
            pieces.append((power, 0.5 / 3))
        energy = sum(min(cap, power * factor) * hours for power, hours in pieces)
        energy *= 1 + 0.05 * sequence.noise()
        if sequence.unit() < 0.05:
            energy *= sequence.uniform(0, 3)
        sample = CalibrationSample(start, start + timedelta(minutes=30), max(0.0, energy), ())
        rows.append(numerics._Row(date(2026, 1, 1), sample, tuple(pieces), 0.5))
    return rows, sequence.choice((cap, 1.3 * cap, 4 * cap)), cap


def daily_samples(days: int, per_hour: int = 2) -> tuple[CalibrationSample, ...]:
    step = timedelta(minutes=60 // per_hour)
    rows = []
    count = 0
    for day in range(days):
        for slot in range(24 * per_hour):
            begin = START + timedelta(days=day) + slot * step
            shape = max(0.0, math.sin(math.pi * (slot / per_hour - 6) / 12))
            count += 1
            power = 130 * shape * (0.8 + 0.2 * ((count * 7919) % 101) / 101)
            energy = (
                min(100, 0.85 * power)
                * step.total_seconds()
                / 3600
                * (1 + 0.03 * (((count * 104729) % 13) - 6) / 6)
            )
            rows.append(
                CalibrationSample(
                    begin, begin + step, energy, (PowerInterval(begin, begin + step, power),)
                )
            )
    return tuple(rows)


def fit_days(days: int) -> FitResult:
    return numerics.fit_calibration(
        daily_samples(days), timezone="UTC", normalization_mw=130, limit_mw=100
    )


def digest(result: FitResult) -> str:
    payload = {
        "status": result.status,
        "factor": result.factor,
        "assessment": result.assessment,
        "report": result.report,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def executed_lines[T](call: Callable[[], T]) -> tuple[int, T]:
    """Python lines executed in `pyracast.plants.numerics`: a deterministic work measure."""
    monitoring = sys.monitoring
    tool = 4
    monitoring.use_tool_id(tool, "fit-cost")
    count = 0

    def line(code: object, _: int) -> object:
        nonlocal count
        if getattr(code, "co_filename", None) == numerics.__file__:
            count += 1
            return None
        return monitoring.DISABLE

    monitoring.register_callback(tool, monitoring.events.LINE, line)
    monitoring.set_events(tool, monitoring.events.LINE)
    try:
        result = call()
    finally:
        monitoring.set_events(tool, 0)
        monitoring.register_callback(tool, monitoring.events.LINE, None)
        monitoring.free_tool_id(tool)
        monitoring.restart_events()
    return count, result
