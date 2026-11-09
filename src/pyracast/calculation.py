from datetime import date, datetime
from typing import cast

from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import SiteBinding
from pyracast.irradiance import (
    BaselineConfiguration,
    ForecastVintage,
    PowerBasis,
    PowerInterval,
    estimate_intervals,
    integrate_day,
)


def _interval(row: dict[str, object]) -> PowerInterval:
    start, end = row.get("start"), row.get("end")
    power, sunlight = row.get("power_kw"), row.get("sunlight", "unknown")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("Normalized interval timestamps must be ISO strings")
    if power is not None and (isinstance(power, bool) or not isinstance(power, int | float)):
        raise ValueError("Normalized power must be numeric or missing")
    if sunlight not in ("day", "night", "partial", "unknown"):
        raise ValueError("Invalid normalized sunlight classification")
    return PowerInterval(
        datetime.fromisoformat(start),
        datetime.fromisoformat(end),
        power,
        sunlight,
    )


def calculate(binding: SiteBinding, target_date: date, data: ProviderData) -> dict[str, object]:
    if data.collected_at is None:
        raise ValueError("The source collection timestamp is required")
    configuration = BaselineConfiguration(
        binding.capacity_kw,
        cast(PowerBasis, binding.power_basis),
        cast(PowerBasis, binding.capacity_basis),
        binding.capacity_semantics_verified,
        binding.tilt_degrees,
    )
    vintage = ForecastVintage(
        binding.provider_account.provider,
        data.source_kind,
        data.collected_at,
        data.issued_at,
        f"{binding.pk}:{binding.region_revision}",
    )
    series = estimate_intervals([_interval(row) for row in data.intervals], configuration, vintage)
    result = integrate_day(series, target_date, binding.timezone)
    flags = set(result.quality_flags)
    if data.raw_payload.get("synthetic") is True:
        flags.add("synthetic_fixture")
    return {
        "region_id": binding.region_id,
        "local_date": result.local_date.isoformat(),
        "timezone": result.timezone,
        "surface": result.surface,
        "start": result.start.isoformat(),
        "end": result.end.isoformat(),
        "energy_kwh_m2": result.energy_kwh_m2,
        "energy_mj_m2": result.energy_mj_m2,
        "partial_energy_kwh_m2": result.partial_energy_kwh_m2,
        "partial_energy_mj_m2": result.partial_energy_mj_m2,
        "coverage_seconds": result.coverage_seconds,
        "valid_coverage_seconds": result.valid_coverage_seconds,
        "coverage_fraction": result.coverage_fraction,
        "complete": result.complete,
        "intervals": [
            {
                "start": interval.start.isoformat(),
                "end": interval.end.isoformat(),
                "ghi_wm2": interval.ghi_wm2,
                "quality_flags": list(interval.quality_flags),
            }
            for interval in result.intervals
        ],
        "vintage": {
            "provider": vintage.provider,
            "source_kind": vintage.source_kind,
            "collected_at": vintage.collected_at.isoformat(),
            "issued_at": vintage.issued_at.isoformat() if vintage.issued_at is not None else None,
            "binding_revision": vintage.binding_revision,
        },
        "configuration": {
            "capacity_kw": configuration.capacity_kw,
            "power_basis": configuration.power_basis,
            "capacity_basis": configuration.capacity_basis,
            "capacity_semantics_verified": configuration.capacity_semantics_verified,
            "tilt_degrees": configuration.tilt_degrees,
        },
        "model_version": result.model_version,
        "quality_flags": sorted(flags),
    }
