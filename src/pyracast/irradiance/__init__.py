from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from typing import Literal
from zoneinfo import ZoneInfo

type PowerBasis = Literal["ac", "dc"]
type Sunlight = Literal["day", "night", "partial", "unknown"]
type SourceKind = Literal["forecast", "estimated_actuals"]

MODEL_VERSION = "power-capacity-proxy-v1"


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Timestamps must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class ForecastVintage:
    provider: str
    source_kind: SourceKind
    collected_at: datetime
    issued_at: datetime | None
    binding_revision: str

    def __post_init__(self) -> None:
        if not self.provider or not self.binding_revision:
            raise ValueError("Provider and binding revision are required")
        if self.source_kind not in ("forecast", "estimated_actuals"):
            raise ValueError("Unsupported source kind")
        _utc(self.collected_at)
        if self.issued_at is not None:
            _utc(self.issued_at)


@dataclass(frozen=True)
class BaselineConfiguration:
    capacity_kw: float
    power_basis: PowerBasis
    capacity_basis: PowerBasis
    capacity_semantics_verified: bool
    tilt_degrees: float = 0

    def __post_init__(self) -> None:
        if not isfinite(self.capacity_kw) or self.capacity_kw <= 0:
            raise ValueError("Capacity must be finite and positive, in kW")
        if self.power_basis not in ("ac", "dc") or self.capacity_basis != self.power_basis:
            raise ValueError("Power and capacity require the same explicit AC/DC basis")
        if not self.capacity_semantics_verified:
            raise ValueError("Capacity semantics must be verified before estimation")
        if self.tilt_degrees != 0:
            raise ValueError("The baseline requires a horizontal reference site")


@dataclass(frozen=True)
class PowerInterval:
    start: datetime
    end: datetime
    power_kw: float | None
    sunlight: Sunlight = "unknown"


@dataclass(frozen=True)
class IrradianceInterval:
    start: datetime
    end: datetime
    ghi_wm2: float | None
    quality_flags: tuple[str, ...]


@dataclass(frozen=True)
class IrradianceSeries:
    intervals: tuple[IrradianceInterval, ...]
    configuration: BaselineConfiguration
    vintage: ForecastVintage
    model_version: str = MODEL_VERSION
    surface: Literal["horizontal"] = "horizontal"


@dataclass(frozen=True)
class DailyEnergy:
    local_date: date
    timezone: str
    start: datetime
    end: datetime
    energy_kwh_m2: float | None
    energy_mj_m2: float | None
    partial_energy_kwh_m2: float
    partial_energy_mj_m2: float
    coverage_seconds: float
    valid_coverage_seconds: float
    coverage_fraction: float
    complete: bool
    intervals: tuple[IrradianceInterval, ...]
    configuration: BaselineConfiguration
    vintage: ForecastVintage
    model_version: str
    surface: Literal["horizontal"]
    quality_flags: tuple[str, ...]


def _ordered_intervals[
    T: (PowerInterval, IrradianceInterval),
](intervals: Sequence[T]) -> tuple[T, ...]:
    ordered = tuple(sorted(intervals, key=lambda item: _utc(item.start)))
    previous_end: datetime | None = None
    for interval in ordered:
        start, end = _utc(interval.start), _utc(interval.end)
        if end <= start:
            raise ValueError("Interval end must follow its start")
        if previous_end is not None and start < previous_end:
            raise ValueError("Duplicate or overlapping intervals are not supported")
        previous_end = end
    return ordered


def _estimate(interval: PowerInterval, capacity_kw: float) -> IrradianceInterval:
    flags = ["unvalidated"]
    if interval.sunlight not in ("day", "night", "partial", "unknown"):
        raise ValueError("Unsupported sunlight classification")
    if interval.sunlight in ("partial", "unknown"):
        flags.append("partial_sunlight" if interval.sunlight == "partial" else "sunlight_unknown")
    power = interval.power_kw
    ghi: float | None = None
    if power is None:
        flags.append("missing_power")
    elif not isfinite(power):
        flags.append("nonfinite_power")
    elif power < 0:
        flags.append("negative_power")
    elif power == 0 and interval.sunlight != "night":
        flags.append("ambiguous_zero_power")
    elif power > 0 and interval.sunlight == "night":
        flags.append("night_power_conflict")
    else:
        ghi = power / capacity_kw * 1000
        if not isfinite(ghi):
            ghi = None
            flags.append("nonfinite_irradiance")
    return IrradianceInterval(_utc(interval.start), _utc(interval.end), ghi, tuple(flags))


def estimate_intervals(
    intervals: Sequence[PowerInterval],
    configuration: BaselineConfiguration,
    vintage: ForecastVintage,
) -> IrradianceSeries:
    return IrradianceSeries(
        tuple(
            _estimate(interval, configuration.capacity_kw)
            for interval in _ordered_intervals(intervals)
        ),
        configuration,
        vintage,
    )


def local_day_bounds(local_date: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    start = datetime.combine(local_date, time.min, zone)
    end = datetime.combine(local_date + timedelta(days=1), time.min, zone)
    if end.astimezone(UTC) <= start.astimezone(UTC):
        raise ValueError("The local date has no positive duration")
    return start.astimezone(UTC), end.astimezone(UTC)


def integrate_day(series: IrradianceSeries, local_date: date, timezone: str) -> DailyEnergy:
    start, end = local_day_bounds(local_date, timezone)
    energy = 0.0
    coverage = valid_coverage = timedelta()
    selected: list[IrradianceInterval] = []
    flags = {"unvalidated"}
    for interval in _ordered_intervals(series.intervals):
        overlap_start = max(start, _utc(interval.start))
        overlap_end = min(end, _utc(interval.end))
        if overlap_start >= overlap_end:
            continue
        seconds = (overlap_end - overlap_start).total_seconds()
        selected.append(interval)
        flags.update(interval.quality_flags)
        if overlap_start != _utc(interval.start) or overlap_end != _utc(interval.end):
            flags.add("prorated_interval_mean")
        coverage += overlap_end - overlap_start
        ghi = interval.ghi_wm2
        if ghi is None:
            continue
        if not isfinite(ghi) or ghi < 0:
            raise ValueError("Irradiance must be finite and nonnegative, or missing")
        energy += ghi * (seconds / 3_600_000)
        valid_coverage += overlap_end - overlap_start
    duration = (end - start).total_seconds()
    complete = valid_coverage == end - start
    if not complete:
        flags.add("incomplete_day")
    return DailyEnergy(
        local_date=local_date,
        timezone=timezone,
        start=start,
        end=end,
        energy_kwh_m2=energy if complete else None,
        energy_mj_m2=energy * 3.6 if complete else None,
        partial_energy_kwh_m2=energy,
        partial_energy_mj_m2=energy * 3.6,
        coverage_seconds=coverage.total_seconds(),
        valid_coverage_seconds=valid_coverage.total_seconds(),
        coverage_fraction=valid_coverage.total_seconds() / duration,
        complete=complete,
        intervals=tuple(selected),
        configuration=series.configuration,
        vintage=series.vintage,
        model_version=series.model_version,
        surface=series.surface,
        quality_flags=tuple(sorted(flags)),
    )
