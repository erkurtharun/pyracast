from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isfinite
from typing import Any, cast

from django.core.exceptions import ValidationError

from pyracast.errors import UNEXPECTED
from pyracast.forecasts.capacity_normalization import normalization_provenance
from pyracast.forecasts.faults import (
    decoding,
    finite_numbers,
    snapshot_row,
    stored_data,
)
from pyracast.forecasts.history import HISTORY_SELECTION_POLICY
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.forecasts.projections import saved_day_payload
from pyracast.forecasts.sources import snapshot_rows
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    CalibrationStatus,
    ForecastIssuance,
    Plant,
    PlantConfiguration,
)
from pyracast.plants.numerics import PowerInterval, forecast
from pyracast.plants.transfer import (
    calibration_matches_transfer,
    reference_power_mw,
    tracking_modeled,
)
from pyracast.solar import Daylight, TimeWindows, daylight_energy_windows, daylight_window

RETROSPECTIVE_ACTIVATIONS = 60
SCENARIO_STATUSES = ("estimated", "unknown_impact")
SUMMARY_FIGURES = ("energy_mwh", "partial_energy_mwh", "coverage_fraction")


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
    model_version: str,
    source_intervals: tuple[dict[str, Any], ...] | None = None,
) -> tuple[PowerInterval, ...]:
    result = []
    capacity = configuration.binding.capacity_kw
    if not isfinite(capacity) or capacity <= 0:
        raise ValueError("Invalid reference capacity")
    for row in snapshot_rows(snapshot) if source_intervals is None else source_intervals:
        with snapshot_row(row, snapshot.pk):
            begin, stop, power = _source_row(row)
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


def _source_row(row: object) -> tuple[datetime, datetime, float | None]:
    if not isinstance(row, dict):
        raise TypeError("A source interval is not an object.")
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
    for key in ("original_start", "original_end"):
        datetime.fromisoformat(row.get(key, start))
    return datetime.fromisoformat(start), datetime.fromisoformat(end), power


def _fits(run: CalibrationRun, configuration: PlantConfiguration, as_of: datetime) -> bool:
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
    )


def _usable(run: CalibrationRun, configuration: PlantConfiguration, as_of: datetime) -> bool:
    from pyracast.plants.incident_policy import incidents_unchanged

    return _fits(run, configuration, as_of) and incidents_unchanged(run, as_of)


def calibration_at(configuration: PlantConfiguration, as_of: datetime) -> CalibrationRun | None:
    activation = (
        CalibrationActivation.objects.select_related(
            "calibration__binding", "calibration__configuration"
        )
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


@dataclass(frozen=True)
class CalibrationTimeline:
    """Usable activated runs, newest activation first, up to the newest baseline reset."""

    runs: tuple[tuple[date, CalibrationRun], ...]

    def for_day(self, day: date) -> CalibrationRun | None:
        """A factor describes its training period onward, not earlier seasons."""
        return next((run for start, run in self.runs if start <= day), None)


def calibration_timeline(configuration: PlantConfiguration, as_of: datetime) -> CalibrationTimeline:
    from pyracast.plants.incident_policy import runs_with_unchanged_incidents

    activations = (
        CalibrationActivation.objects.select_related(
            "calibration__binding", "calibration__configuration"
        )
        .defer("calibration__dataset_manifest")
        .filter(configuration=configuration, created_at__lte=as_of)
        .order_by("-created_at", "-pk")[:RETROSPECTIVE_ACTIVATIONS]
    )
    fitted: list[tuple[date, CalibrationRun]] = []
    for activation in activations:
        run = activation.calibration
        if run is None:
            break
        start = _fitted_start(run)
        if start is not None and _fits(run, configuration, as_of):
            fitted.append((start, run))
    return CalibrationTimeline(tuple(runs_with_unchanged_incidents(configuration, fitted, as_of)))


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
    model_version: str,
    source_intervals: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, object]:
    """`model_version` is the transfer the result is computed under and records: a new result
    takes `transfer_model(configuration, calibration)`, a recomputation the stored version."""
    start, end = local_day_bounds(target_date, configuration.binding.timezone)
    selected = snapshot_rows(snapshot) if source_intervals is None else source_intervals
    intervals = power_intervals(
        snapshot, configuration, model_version=model_version, source_intervals=selected
    )
    daylight, energy_windows = forecast_daylight(configuration, target_date, selected)
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
    result["model_version"] = model_version
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
        "lead_hours": (start - snapshot.collected_at).total_seconds() / 3600,
        "issued_at": snapshot.issued_at.isoformat() if snapshot.issued_at else None,
        "calibration_id": calibration.pk if calibration else None,
        "calibration_assessment": calibration.assessment if calibration else "uncalibrated",
        "calibration_cutoff_at": calibration.cutoff_at.isoformat() if calibration else None,
        "calibration_dataset_sha256": calibration.dataset_sha256 if calibration else None,
    }
    return result


def require_resolution(resolution_minutes: int) -> None:
    if isinstance(resolution_minutes, bool) or resolution_minutes not in (30, 60):
        raise ValidationError(UNEXPECTED)


def decoded_issuance(
    issuance: ForecastIssuance, resolution: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The stored result and outage scenario as the reads answer and project them: objects
    whose numbers are finite, since the answer is JSON. They are the stored objects, not
    copies."""
    with decoding("forecast_issuance", issuance.pk):
        payloads, scenarios = issuance.payloads, issuance.outage_scenarios
        if not isinstance(payloads, dict) or not isinstance(scenarios, dict):
            raise TypeError("Stored issuance payloads are not objects.")
        result, scenario = _stored_result(payloads[resolution]), scenarios.get(resolution)
        if scenario is not None:
            _stored_scenario(scenario)
        finite_numbers(result)
        finite_numbers(scenario)
    return result, scenario


def _stored_scenario(scenario: object) -> None:
    if not isinstance(scenario, dict):
        raise TypeError("A stored outage scenario is not an object.")
    if scenario["status"] not in SCENARIO_STATUSES:
        raise ValueError("A stored outage scenario has no known status.")
    _stored_result(scenario["result"])


def _stored_result(result: object) -> dict[str, Any]:
    if not isinstance(result, dict) or not isinstance(result["quality_flags"], list):
        raise TypeError("A stored result is not an object with quality flags.")
    intervals, provenance = result["intervals"], result["provenance"]
    daylight = result.get("daylight")
    if not isinstance(intervals, list) or not isinstance(provenance, dict):
        raise TypeError("A stored result has no interval array or provenance object.")
    if daylight is not None and not isinstance(daylight, dict):
        raise TypeError("A stored daylight summary is not an object.")
    for summary in (result, daylight or {}):
        for key in SUMMARY_FIGURES:
            _stored_figure(summary.get(key))
    if provenance.get("collected_at") is not None:
        _stored_instant(provenance["collected_at"])
    for row in intervals:
        if not isinstance(row, dict):
            raise TypeError("A stored interval is not an object.")
        _stored_instant(row["start"])
        _stored_instant(row["end"])
        _stored_figure(row.get("energy_mwh"))
    return result


def _stored_figure(value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("A stored figure is not a number.")
    if not isfinite(float(value)):
        raise ValueError("A stored figure is not finite.")


def _stored_instant(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("A stored instant is not text.")
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() is None:
        raise ValueError("A stored instant has no UTC offset.")
    moment.astimezone(UTC)


def issued_forecast(
    plant: Plant,
    target_date: date,
    resolution_minutes: int,
    now: datetime,
    *,
    binding_id: int | None = None,
) -> dict[str, object]:
    from pyracast.plants.forecast_archive import select_forecast_issuance

    issuance = select_forecast_issuance(plant, target_date, as_of=now, binding_id=binding_id)
    if issuance is None:
        return missing_forecast(plant, target_date, now, binding_id=binding_id)
    return issuance_outlook(plant, issuance, target_date, resolution_minutes, now)


def missing_forecast(
    plant: Plant, target_date: date, now: datetime, *, binding_id: int | None = None
) -> dict[str, object]:
    from pyracast.plants.forecast_archive import forecast_cutoff

    configuration = next(
        (
            candidate
            for candidate in PlantConfiguration.objects.select_related("binding")
            .filter(plant=plant, created_at__lte=now)
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
        "as_of": now.astimezone(UTC).isoformat(),
        "configuration_id": configuration.pk if configuration else None,
        "snapshot_id": None,
        "issuance_id": None,
        "calibration_id": None,
        "source_kind": "forecast",
        "freshness": "missing",
        "stale_reason": None,
        "unavailable_reason": "forecast_unavailable"
        if configuration is not None
        else "configuration_unavailable",
        "result": None,
        "regional_irradiance": None,
        "outage_scenario": None,
    }


def issuance_outlook(
    plant: Plant,
    issuance: ForecastIssuance,
    target_date: date,
    resolution_minutes: int,
    now: datetime,
) -> dict[str, object]:
    configuration, snapshot = issuance.configuration, issuance.snapshot
    with stored_data("plant", plant.pk, target_date, "archived_forecast"):
        stored, stored_scenario = decoded_issuance(issuance, str(resolution_minutes))
    result, scenario = deepcopy(stored), deepcopy(stored_scenario)
    flags = cast(list[str], result["quality_flags"])
    flags.append("archived_forecast")
    if configuration.pk != plant.current_configuration_id:
        flags.append("previous_configuration")
    regional = saved_day_payload(snapshot, target_date)
    return {
        "plant_id": plant.pk,
        "date": target_date.isoformat(),
        "timezone": configuration.binding.timezone,
        "as_of": min(now, issuance.cutoff_at).astimezone(UTC).isoformat(),
        "configuration_id": configuration.pk,
        "snapshot_id": snapshot.pk,
        "issuance_id": issuance.pk,
        "calibration_id": issuance.calibration_id,
        "source_kind": "forecast",
        "freshness": "archived",
        "stale_reason": None,
        "unavailable_reason": None,
        "result": result,
        "regional_irradiance": {
            key: regional.get(key)
            for key in ("energy_kwh_m2", "partial_energy_kwh_m2", "complete", "quality_flags")
        }
        if regional is not None
        else None,
        "outage_scenario": scenario,
    }
