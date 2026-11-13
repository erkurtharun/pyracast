from dataclasses import dataclass
from math import isfinite, sqrt

from pyracast.irradiance import SourceKind, Sunlight

type Coefficients = tuple[float, float, float, float, float, float]


@dataclass(frozen=True)
class QuadraticModel:
    coefficients: Coefficients
    min_temperature_c: float
    max_temperature_c: float
    max_ghi_wm2: float = 1400
    min_transmission: float = 0.05
    version: str = "quadratic-weather-inverse-v1"

    def __post_init__(self) -> None:
        if len(self.coefficients) != 6 or not all(isfinite(c) for c in self.coefficients):
            raise ValueError("Six finite quadratic coefficients are required")
        if (
            not isfinite(self.min_temperature_c)
            or not isfinite(self.max_temperature_c)
            or self.min_temperature_c > self.max_temperature_c
        ):
            raise ValueError("An ordered finite training temperature range is required")
        if not isfinite(self.max_ghi_wm2) or self.max_ghi_wm2 <= 0:
            raise ValueError("A positive finite irradiance support limit is required")
        if not isfinite(self.min_transmission) or not 0 < self.min_transmission <= 1:
            raise ValueError("Minimum snow transmission must be in (0, 1]")
        if not self.version:
            raise ValueError("A model version is required")


@dataclass(frozen=True)
class InverseEstimate:
    ghi_wm2: float | None
    sensitivity_wm2_per_kw: float | None
    quality_flags: tuple[str, ...]


def _terms(model: QuadraticModel, temperature_c: float) -> tuple[float, float, float]:
    c0, c1, c2, c3, c4, c5 = model.coefficients
    t = (temperature_c - 25) / 25
    return c4, c1 + c3 * t, c0 + c2 * t + c5 * t * t


def forward_power(
    ghi_wm2: float,
    *,
    capacity_kw: float,
    air_temp_c: float,
    snow_loss_fraction: float,
    model: QuadraticModel,
) -> float:
    if (
        not isfinite(ghi_wm2)
        or ghi_wm2 < 0
        or not isfinite(capacity_kw)
        or capacity_kw <= 0
        or not isfinite(air_temp_c)
        or not isfinite(snow_loss_fraction)
        or not 0 <= snow_loss_fraction <= 1
    ):
        raise ValueError("Finite irradiance, temperature, capacity and snow loss are required")
    a, b, c = _terms(model, air_temp_c)
    g = ghi_wm2 / 1000
    return capacity_kw * (1 - snow_loss_fraction) * max(0, a * g * g + b * g + c)


def reconstruct_ghi(
    power_kw: float | None,
    *,
    capacity_kw: float,
    air_temp_c: float | None,
    snow_loss_fraction: float | None,
    model: QuadraticModel,
    power_is_capped: bool = False,
    source_kind: SourceKind = "estimated_actuals",
    sunlight: Sunlight = "unknown",
) -> InverseEstimate:
    if isinstance(capacity_kw, bool) or not isfinite(capacity_kw) or capacity_kw <= 0:
        raise ValueError("A positive finite AC reference capacity is required")
    if source_kind not in ("forecast", "estimated_actuals"):
        raise ValueError("Unsupported source kind")
    if sunlight not in ("day", "night", "partial", "unknown"):
        raise ValueError("Unsupported sunlight classification")
    flags = ["empirical_inverse", "aggregation_approximation", "historical_calibration"]
    if source_kind == "forecast":
        flags.append("forecast_transfer_unvalidated")

    def unavailable(reason: str) -> InverseEstimate:
        return InverseEstimate(None, None, (*flags, reason))

    if power_kw is None:
        return unavailable("missing_power")
    if isinstance(power_kw, bool) or not isfinite(power_kw) or power_kw < 0:
        return unavailable("invalid_power")
    if sunlight == "night":
        if power_kw != 0:
            return unavailable("night_power_conflict")
        return InverseEstimate(0, None, (*flags, "declared_night"))
    if power_kw == 0:
        return unavailable("zero_power_not_identifiable")
    if power_is_capped:
        return unavailable("capped_power_not_identifiable")
    if air_temp_c is None or snow_loss_fraction is None:
        return unavailable("weather_inputs_required")
    if (
        isinstance(air_temp_c, bool)
        or isinstance(snow_loss_fraction, bool)
        or not isfinite(air_temp_c)
        or not isfinite(snow_loss_fraction)
        or not 0 <= snow_loss_fraction <= 1
    ):
        return unavailable("invalid_weather")
    transmission = 1 - snow_loss_fraction
    if transmission == 0:
        return unavailable("snow_opaque")
    if transmission < model.min_transmission:
        return unavailable("snow_ill_conditioned")
    if not model.min_temperature_c <= air_temp_c <= model.max_temperature_c:
        flags.append("temperature_extrapolation")
    a, b, c = _terms(model, air_temp_c)
    g_max = model.max_ghi_wm2 / 1000
    if not all(isfinite(value) for value in (a, b, c)):
        return unavailable("numerical_failure")
    if min(b, b + 2 * a * g_max) <= 0:
        return unavailable("nonmonotone_model_domain")
    target = power_kw / capacity_kw / transmission
    discriminant = b * b - 4 * a * (c - target)
    if not isfinite(discriminant) or discriminant < 0:
        return unavailable("no_supported_root")
    # Rationalized ascending root avoids subtracting nearly equal numbers; also covers a=0.
    g = 2 * (target - c) / (b + sqrt(discriminant))
    if not isfinite(g) or g < 0 or g > g_max + 1e-9:
        return unavailable("irradiance_out_of_domain")
    g = min(g, g_max)
    slope = b + 2 * a * g
    sensitivity = 1000 / capacity_kw / transmission / slope
    residual = abs(a * g * g + b * g + c - target)
    if not isfinite(sensitivity) or residual > 1e-9 * max(1, target):
        return unavailable("numerical_failure")
    return InverseEstimate(g * 1000, sensitivity, tuple(flags))
