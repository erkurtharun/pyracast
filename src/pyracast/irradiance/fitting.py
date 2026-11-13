from collections.abc import Sequence
from dataclasses import dataclass
from math import fsum, hypot, isfinite

from pyracast.irradiance.pairs import PairedInterval

Coefficients = tuple[float, float, float, float, float, float]


@dataclass(frozen=True)
class FitResult:
    coefficients: Coefficients
    sample_count: int
    min_temperature_c: float
    max_temperature_c: float


def _features(sample: PairedInterval) -> tuple[Coefficients, float, float] | None:
    if not isfinite(sample.power_kw) or sample.power_kw < 0:
        raise ValueError("Power must be finite and nonnegative")
    if len(sample.weather) != 6 or (sample.end - sample.start).total_seconds() != 1800:
        raise ValueError("Expected a complete 30-minute pair with six weather intervals")
    columns: list[list[float]] = [[] for _ in range(6)]
    temperatures: list[float] = []
    previous = sample.start
    eligible = True
    for weather in sample.weather:
        if weather.start != previous or (weather.end - weather.start).total_seconds() != 300:
            raise ValueError("Weather intervals must cover the pair contiguously")
        previous = weather.end
        if (
            not all(
                isfinite(value)
                for value in (weather.ghi_wm2, weather.air_temp_c, weather.snow_loss_fraction)
            )
            or weather.ghi_wm2 < 0
            or not 0 <= weather.snow_loss_fraction <= 1
        ):
            raise ValueError("Invalid native weather value")
        transmission = 1 - weather.snow_loss_fraction
        eligible &= weather.ghi_wm2 > 25 and transmission >= 0.05
        g = weather.ghi_wm2 / 1000
        t = (weather.air_temp_c - 25) / 25
        values = (1.0, g, t, g * t, g * g, t * t)
        if not all(isfinite(value) for value in values):
            raise ValueError("Native weather features overflow")
        for column, value in zip(columns, values, strict=True):
            column.append(transmission * value / 6)
        temperatures.append(weather.air_temp_c)
    if not eligible:
        return None
    values_mean = [fsum(column) for column in columns]
    return (
        (
            values_mean[0],
            values_mean[1],
            values_mean[2],
            values_mean[3],
            values_mean[4],
            values_mean[5],
        ),
        min(temperatures),
        max(temperatures),
    )


def _least_squares(rows: Sequence[Coefficients], targets: Sequence[float]) -> Coefficients:
    columns = [[row[index] for row in rows] for index in range(6)]
    scales = [hypot(*column) for column in columns]
    if any(scale == 0 or not isfinite(scale) for scale in scales):
        raise ValueError("Training features are rank deficient")
    orthogonal: list[list[float]] = []
    triangular = [[0.0] * 6 for _ in range(6)]
    for index, (column, scale) in enumerate(zip(columns, scales, strict=True)):
        residual = [value / scale for value in column]
        # Reorthogonalization avoids the conditioning loss of normal equations.
        for _ in range(2):
            for previous, vector in enumerate(orthogonal):
                projection = fsum(a * b for a, b in zip(vector, residual, strict=True))
                triangular[previous][index] += projection
                residual = [
                    value - projection * component
                    for value, component in zip(residual, vector, strict=True)
                ]
        norm = hypot(*residual)
        if norm <= 1e-10:
            raise ValueError("Training features are rank deficient")
        triangular[index][index] = norm
        orthogonal.append([value / norm for value in residual])
    target_scale = max(targets)
    scaled_targets = [target / target_scale for target in targets]
    projected = [
        fsum(value * target for value, target in zip(vector, scaled_targets, strict=True))
        for vector in orthogonal
    ]
    solution = [0.0] * 6
    for index in reversed(range(6)):
        remainder = fsum(triangular[index][j] * solution[j] for j in range(index + 1, 6))
        solution[index] = (projected[index] - remainder) / triangular[index][index]
    result = [value / scale * target_scale for value, scale in zip(solution, scales, strict=True)]
    if not all(isfinite(value) for value in result):
        raise ValueError("Fitted coefficients overflow")
    return result[0], result[1], result[2], result[3], result[4], result[5]


def fit_quadratic(samples: Sequence[PairedInterval], *, capacity_kw: float) -> FitResult:
    if not isfinite(capacity_kw) or capacity_kw <= 0:
        raise ValueError("Capacity must be finite and positive")
    rows: list[Coefficients] = []
    targets: list[float] = []
    temperatures: list[float] = []
    for sample in samples:
        features = _features(sample)
        if features is None or sample.power_kw == 0:
            continue
        row, minimum, maximum = features
        rows.append(row)
        target = sample.power_kw / capacity_kw
        if not isfinite(target):
            raise ValueError("Normalized power overflows")
        targets.append(target)
        temperatures.extend((minimum, maximum))
    if len(rows) < 6:
        raise ValueError("At least six eligible training pairs are required")
    return FitResult(_least_squares(rows, targets), len(rows), min(temperatures), max(temperatures))
