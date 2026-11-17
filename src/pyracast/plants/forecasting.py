from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import Any, cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone

from pyracast.accounts.operations import require_member
from pyracast.forecasts.capacity_normalization import normalization_provenance
from pyracast.forecasts.history import HISTORY_SELECTION_POLICY
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.forecasts.projections import saved_day_payload
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    CalibrationStatus,
    Plant,
    PlantConfiguration,
)
from pyracast.plants.numerics import PowerInterval, forecast
from pyracast.plants.transfer import (
    calibration_matches_transfer,
    reference_power_mw,
    tracking_modeled,
    transfer_model,
)
from pyracast.regions.models import Region
from pyracast.solar import Daylight, TimeWindows, daylight_energy_windows, daylight_window

RETROSPECTIVE_ACTIVATIONS = 60


def power_limit(configuration: PlantConfiguration) -> float | None:
    limits = [configuration.inverter_capacity_mw]
    if configuration.measurement_basis == "net_export":
        limits.append(configuration.export_limit_mw)
    known = [value for value in limits if value is not None]
    return min(known) if known else None


def power_intervals(
    snapshot: ForecastSnapshot,
    configuration: PlantConfiguration,
    *,
    source_intervals: tuple[dict[str, Any], ...] | None = None,
    model_version: str | None = None,
) -> tuple[PowerInterval, ...]:
    result = []
    capacity = configuration.binding.capacity_kw
    if not isfinite(capacity) or capacity <= 0:
        raise ValueError("Invalid reference capacity")
    for row in snapshot.normalized_intervals if source_intervals is None else source_intervals:
        start, end, power = row.get("start"), row.get("end"), row.get("power_kw")
        if not isinstance(start, str) or not isinstance(end, str):
            raise ValueError("Invalid source interval timestamps")
        if power is not None and (
            isinstance(power, bool)
            or not isinstance(power, int | float)
            or not isfinite(power)
            or power < 0
        ):
            raise ValueError("Invalid source power")
        begin, stop = datetime.fromisoformat(start), datetime.fromisoformat(end)
        result.append(
            PowerInterval(
                begin,
                stop,
                None
                if power is None
                else reference_power_mw(
                    configuration, begin, stop, power, model_version=model_version
                ),
            )
        )
    return tuple(result)


def _usable(run: CalibrationRun, configuration: PlantConfiguration, as_of: datetime) -> bool:
    from pyracast.plants.incident_policy import incidents_unchanged

    return (
        run.configuration_id == configuration.pk
        and run.binding_id == configuration.binding_id
        and run.plant_id == configuration.plant_id
        and run.status == CalibrationStatus.READY
        and run.factor is not None
        and run.completed_at is not None
        and run.completed_at <= as_of
        and run.cutoff_at <= as_of
        and run.created_at <= as_of
        and calibration_matches_transfer(run, configuration)
        and incidents_unchanged(run, as_of)
    )


def calibration_at(configuration: PlantConfiguration, as_of: datetime) -> CalibrationRun | None:
    activation = (
        CalibrationActivation.objects.select_related("calibration")
        .filter(configuration=configuration, created_at__lte=as_of)
        .order_by("-created_at", "-pk")
        .first()
    )
    if activation is None or activation.calibration is None:
        return None
    run = activation.calibration
    return run if _usable(run, configuration, as_of) else None


def _fitted_start(run: CalibrationRun) -> date | None:
    days = run.result.get("training_day_dates")
    if not isinstance(days, list) or not days or not isinstance(days[0], str):
        return None
    try:
        return date.fromisoformat(days[0])
    except ValueError:
        return None


def calibration_for_day(
    configuration: PlantConfiguration, day: date, as_of: datetime
) -> CalibrationRun | None:
    """Activated calibration whose training period had begun by a completed `day`.

    A factor describes its training period onward, not earlier seasons. The newest
    qualifying activation wins, and a baseline reset ends the search.
    """
    activations = (
        CalibrationActivation.objects.select_related("calibration")
        .filter(configuration=configuration, created_at__lte=as_of)
        .order_by("-created_at", "-pk")[:RETROSPECTIVE_ACTIVATIONS]
    )
    for activation in activations:
        run = activation.calibration
        if run is None:
            return None
        start = _fitted_start(run)
        if start is not None and start <= day and _usable(run, configuration, as_of):
            return run
    return None


def forecast_daylight(
    configuration: PlantConfiguration,
    target_date: date,
    source_intervals: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> tuple[Daylight, TimeWindows]:
    binding = configuration.binding
    daylight = daylight_window(target_date, binding.timezone, binding.latitude, binding.longitude)
    windows = daylight_energy_windows(
        daylight,
        (
            (
                datetime.fromisoformat(row.get("original_start", row["start"])),
                datetime.fromisoformat(row.get("original_end", row["end"])),
            )
            for row in source_intervals
        ),
    )
    return daylight, windows


def forecast_payload(
    configuration: PlantConfiguration,
    snapshot: ForecastSnapshot,
    calibration: CalibrationRun | None,
    target_date: date,
    resolution_minutes: int,
    *,
    source_intervals: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, object]:
    start, end = local_day_bounds(target_date, configuration.binding.timezone)
    intervals = power_intervals(snapshot, configuration, source_intervals=source_intervals)
    daylight, energy_windows = forecast_daylight(
        configuration,
        target_date,
        snapshot.normalized_intervals if source_intervals is None else source_intervals,
    )
    result = forecast(
        intervals,
        start=start,
        end=end,
        resolution_minutes=resolution_minutes,
        limit_mw=power_limit(configuration),
        factor=calibration.factor
        if calibration is not None and calibration.factor is not None
        else 1,
        daylight=daylight,
        energy_windows=energy_windows,
    )
    flags = list(result["quality_flags"]) if isinstance(result["quality_flags"], list) else []
    result["model_version"] = transfer_model(configuration)
    flags.extend(
        (
            "unvalidated_reference_transfer",
            "assumed_tracker_geometry"
            if tracking_modeled(configuration)
            else "geometry_not_modeled",
        )
    )
    if snapshot.source_kind == "estimated_actuals":
        flags.append("estimated_actuals_not_metered")
        selected = snapshot.normalized_intervals if source_intervals is None else source_intervals
        if configuration.created_at > start and any(
            row.get("source_kind", snapshot.source_kind) == "estimated_actuals"
            and datetime.fromisoformat(row.get("original_start", row["start"]))
            < min(configuration.created_at, end)
            and datetime.fromisoformat(row.get("original_end", row["end"])) > start
            for row in selected
        ):
            flags.append("historical_configuration_assumed")
    if calibration is None:
        flags.append("uncalibrated")
    elif calibration.result.get("provisional_production"):
        flags.append("provisional_production_calibration")
    if power_limit(configuration) is None:
        flags.append("output_limit_unknown")
    result["quality_flags"] = flags
    result["configuration"] = {
        "id": configuration.pk,
        "revision": configuration.revision,
        "dc_capacity_mw": configuration.dc_capacity_mw,
        "inverter_capacity_mw": configuration.inverter_capacity_mw,
        "export_limit_mw": configuration.export_limit_mw,
        "measurement_basis": configuration.measurement_basis,
    }
    result["provenance"] = {
        "selection_policy": HISTORY_SELECTION_POLICY
        if snapshot.source_kind == "estimated_actuals"
        else "single-snapshot-forecast-projection-v1",
        "snapshot_id": snapshot.pk,
        "binding_id": configuration.binding_id,
        "capacity_normalization": normalization_provenance(snapshot),
        "source_kind": snapshot.source_kind,
        "collected_at": snapshot.collected_at.isoformat(),
        "scheduled_slot": snapshot.job.scheduled_slot.isoformat(),
        "late": snapshot.collected_at > snapshot.job.scheduled_slot,
        "lead_hours": (start - snapshot.collected_at).total_seconds() / 3600,
        "issued_at": snapshot.issued_at.isoformat() if snapshot.issued_at else None,
        "calibration_id": calibration.pk if calibration else None,
        "calibration_assessment": calibration.assessment if calibration else "uncalibrated",
        "calibration_cutoff_at": calibration.cutoff_at.isoformat() if calibration else None,
        "calibration_dataset_sha256": calibration.dataset_sha256 if calibration else None,
    }
    return result


def archived_forecast(
    plant: Plant,
    target_date: date,
    resolution_minutes: int,
    now: datetime,
    as_of: datetime | None = None,
    *,
    binding_id: int | None = None,
) -> dict[str, object]:
    today = now.astimezone(ZoneInfo(plant.region.timezone)).date()
    if not today - timedelta(days=366) <= target_date <= today:
        raise ValidationError("date_out_of_range")
    return _issued_forecast(
        plant, target_date, resolution_minutes, now, as_of=as_of, binding_id=binding_id
    )


def _issued_forecast(
    plant: Plant,
    target_date: date,
    resolution_minutes: int,
    now: datetime,
    *,
    as_of: datetime | None = None,
    binding_id: int | None = None,
) -> dict[str, object]:
    from pyracast.plants.forecast_archive import forecast_cutoff, select_forecast_issuance

    if isinstance(resolution_minutes, bool) or resolution_minutes not in (30, 60):
        raise ValidationError("Resolution must be 30 or 60 minutes.")
    if as_of is not None and (as_of.utcoffset() is None or as_of > now):
        raise ValidationError("as_of must be aware and no later than now.")
    cutoff = (as_of or now).astimezone(UTC)
    issuance = select_forecast_issuance(plant, target_date, as_of=cutoff, binding_id=binding_id)
    if issuance is None:
        configuration = next(
            (
                candidate
                for candidate in PlantConfiguration.objects.select_related("binding")
                .filter(plant=plant, created_at__lte=cutoff)
                .order_by("-created_at", "-pk")
                if candidate.created_at < forecast_cutoff(target_date, candidate.binding)
                and (binding_id is None or candidate.binding_id == binding_id)
            ),
            None,
        )
        return {
            "plant_id": plant.pk,
            "date": target_date.isoformat(),
            "timezone": plant.region.timezone,
            "as_of": cutoff.isoformat(),
            "configuration_id": configuration.pk if configuration else None,
            "snapshot_id": None,
            "issuance_id": None,
            "calibration_id": None,
            "source_kind": "forecast",
            "freshness": "missing",
            "unavailable_reason": "forecast_unavailable"
            if configuration is not None
            else "configuration_unavailable",
            "result": None,
            "regional_irradiance": None,
            "outage_scenario": None,
        }
    configuration, snapshot = issuance.configuration, issuance.snapshot
    result = deepcopy(issuance.payloads[str(resolution_minutes)])
    flags = cast(list[str], result["quality_flags"])
    flags.append("archived_forecast")
    if configuration.pk != plant.current_configuration_id:
        flags.append("previous_configuration")
    regional = saved_day_payload(snapshot, target_date)
    return {
        "plant_id": plant.pk,
        "date": target_date.isoformat(),
        "timezone": configuration.binding.timezone,
        "as_of": (as_of or min(now, issuance.cutoff_at)).astimezone(UTC).isoformat(),
        "configuration_id": configuration.pk,
        "snapshot_id": snapshot.pk,
        "issuance_id": issuance.pk,
        "calibration_id": issuance.calibration_id,
        "source_kind": "forecast",
        "freshness": "fresh" if configuration.pk == plant.current_configuration_id else "stale",
        "unavailable_reason": None,
        "result": result,
        "regional_irradiance": {
            key: regional.get(key)
            for key in ("energy_kwh_m2", "partial_energy_kwh_m2", "complete", "quality_flags")
        }
        if regional is not None
        else None,
        "outage_scenario": deepcopy(issuance.outage_scenarios.get(str(resolution_minutes))),
    }


def plant_forecast(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    target_date: date,
    resolution_minutes: int,
    as_of: datetime | None = None,
) -> dict[str, object]:
    require_member(actor, tenant_id)
    region = Region.objects.get(pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True)
    plant = Plant.objects.get(pk=plant_id, region=region)
    now = timezone.now()
    today = now.astimezone(ZoneInfo(region.timezone)).date()
    if not today - timedelta(days=366) <= target_date <= today + timedelta(days=14):
        raise ValidationError("date_out_of_range")
    return _issued_forecast(plant, target_date, resolution_minutes, now, as_of=as_of)
