from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.incidents.operations import select_incidents
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_sources import apply_composition, replay_composition
from pyracast.plants.forecasting import forecast_payload, power_intervals, power_limit
from pyracast.plants.incident_policy import incidents_unchanged
from pyracast.plants.measurements import production_identity_current
from pyracast.plants.models import CalibrationRun, PlantConfiguration
from pyracast.plants.numerics import DERATING_LOOKBACK_DAYS, POLICY_VERSION, forecast
from pyracast.plants.operating_limits import OperatingLimit, affected_power
from pyracast.plants.transfer import calibration_matches_transfer

DERATING_SCENARIO_POLICY = "suspected-derating-v1"


def _limited_forecast(
    normal: dict[str, Any],
    configuration: PlantConfiguration,
    snapshot: ForecastSnapshot,
    source_intervals: tuple[dict[str, Any], ...] | None,
    limits: tuple[OperatingLimit, ...],
    bounds: tuple[datetime, datetime],
    resolution_minutes: int,
) -> dict[str, Any]:
    result: dict[str, Any] = forecast(
        affected_power(
            power_intervals(
                snapshot,
                configuration,
                source_intervals=source_intervals,
                model_version=normal["model_version"],
            ),
            limits,
            dc_capacity_mw=configuration.dc_capacity_mw,
            normal_limit_mw=power_limit(configuration),
            factor=normal["factor"],
            net_export=configuration.measurement_basis == "net_export",
        ),
        start=bounds[0],
        end=bounds[1],
        resolution_minutes=resolution_minutes,
        limit_mw=None,
    )
    for key in ("factor", "limit_mw", "model_version", "configuration"):
        result[key] = normal[key]
    if source_intervals is not None:
        source_ids = {row["start"]: row["source_snapshot_ids"] for row in normal["intervals"]}
        for row in result["intervals"]:
            row["source_snapshot_ids"] = source_ids[row["start"]]
    return result


def derating_scenario(
    outlook: dict[str, Any], target_date: date, resolution_minutes: int, known_at: datetime
) -> dict[str, object] | None:
    """Conditional outlook while recent production sits below the active calibration."""
    normal: dict[str, Any] | None = outlook.get("result")
    if normal is None or outlook["calibration_id"] is None or outlook["source_kind"] != "forecast":
        return None
    configuration = PlantConfiguration.objects.select_related("binding").get(
        pk=outlook["configuration_id"], plant_id=outlook["plant_id"]
    )
    zone = configuration.binding.timezone
    start, end = local_day_bounds(target_date, zone)
    run = (
        CalibrationRun.objects.select_related("configuration", "binding")
        .filter(
            configuration=configuration,
            status__in=("ready", "rejected"),
            completed_at__lte=known_at,
            result__policy_version=POLICY_VERSION,
        )
        .order_by("-completed_at", "-pk")
        .first()
    )
    assessment = run.result.get("derating") if run is not None else None
    evidence_end = assessment.get("window_end") if isinstance(assessment, dict) else None
    if (
        run is None
        or not isinstance(assessment, dict)
        or assessment.get("status") != "suspected"
        or assessment.get("baseline_factor") != normal["factor"]
        or end <= known_at
        or not isinstance(evidence_end, str)
        or date.fromisoformat(evidence_end)
        < known_at.astimezone(ZoneInfo(zone)).date() - timedelta(days=DERATING_LOOKBACK_DAYS)
        or not calibration_matches_transfer(run, configuration)
        or not production_identity_current(run)
        or not incidents_unchanged(run, known_at)
    ):
        return None
    ratio = assessment["performance_ratio"]
    inverter = configuration.inverter_capacity_mw
    # An export limit is a grid constraint; only inverter capacity follows lost units.
    scaled_limit = None if inverter is None or assessment["limit_reached"] else ratio * inverter
    snapshot = ForecastSnapshot.objects.get(
        pk=normal["provenance"]["snapshot_id"], job__binding_id=configuration.binding_id
    )
    selected_sources = replay_composition(normal["provenance"], configuration, start, end)
    result = _limited_forecast(
        normal,
        configuration,
        snapshot,
        selected_sources[0].intervals if selected_sources is not None else None,
        (OperatingLimit(start, None, ratio * configuration.dc_capacity_mw, scaled_limit, None),),
        (start, end),
        resolution_minutes,
    )
    result["provenance"] = {
        **normal["provenance"],
        "scenario_policy": DERATING_SCENARIO_POLICY,
        "evaluated_at": known_at.isoformat(),
        "derating_calibration_id": run.pk,
    }
    result["quality_flags"] = list(
        dict.fromkeys(
            normal["quality_flags"]
            + result["quality_flags"]
            + ["conditional_on_suspected_derating"]
        )
    )
    return {
        "evaluated_at": known_at.isoformat(),
        "status": "suspected",
        "calibration_id": run.pk,
        "performance_ratio": ratio,
        "threshold": assessment["threshold"],
        "window_start": assessment["window_start"],
        "window_end": assessment["window_end"],
        "days": assessment["days"],
        "inverter_limit_scaled": scaled_limit is not None,
        "result": result,
    }


def incident_scenario(
    outlook: dict[str, Any], target_date: date, resolution_minutes: int, known_at: datetime
) -> dict[str, object] | None:
    normal: dict[str, Any] | None = outlook.get("result")
    if normal is None:
        return None
    configuration = PlantConfiguration.objects.select_related("binding").get(
        pk=outlook["configuration_id"], plant_id=outlook["plant_id"]
    )
    start, end = local_day_bounds(target_date, configuration.binding.timezone)
    snapshot = ForecastSnapshot.objects.get(
        pk=normal["provenance"]["snapshot_id"], job__binding_id=configuration.binding_id
    )
    selected_sources = replay_composition(normal["provenance"], configuration, start, end)
    source_intervals = selected_sources[0].intervals if selected_sources is not None else None
    if outlook["calibration_id"] is not None:
        calibration = CalibrationRun.objects.get(
            pk=outlook["calibration_id"], configuration=configuration
        )
        if not incidents_unchanged(calibration, known_at):
            normal = cast(
                dict[str, Any],
                forecast_payload(
                    configuration,
                    snapshot,
                    None,
                    target_date,
                    resolution_minutes,
                    source_intervals=source_intervals,
                ),
            )
            if selected_sources is not None:
                apply_composition(normal, selected_sources[1], selected_sources[0])
            normal["quality_flags"].append("calibration_invalidated_by_outage_history")
            outlook["result"] = normal
            outlook["calibration_id"] = None
    incidents = select_incidents(configuration.plant_id, start, end, known_at)
    if not incidents or (
        known_at < end
        and not any(
            row.starts_at <= known_at and (row.ends_at is None or row.ends_at > known_at)
            for row in incidents
        )
    ):
        return None
    limits = tuple(
        OperatingLimit(
            row.starts_at,
            row.ends_at,
            row.available_dc_mw if row.declared_configuration_id == configuration.pk else None,
            row.available_inverter_mw
            if row.declared_configuration_id == configuration.pk
            else None,
            row.available_export_mw if row.declared_configuration_id == configuration.pk else None,
        )
        for row in incidents
    )
    result = _limited_forecast(
        normal, configuration, snapshot, source_intervals, limits, (start, end), resolution_minutes
    )
    unknown = any(
        row.declared_configuration_id != configuration.pk
        or (
            row.available_dc_mw is None
            and row.available_inverter_mw is None
            and row.available_export_mw is None
        )
        for row in incidents
    )
    result["provenance"] = {
        **normal["provenance"],
        "scenario_policy": "reported-operating-limits-v1",
        "evaluated_at": known_at.isoformat(),
        "incident_revision_ids": [row.revision_id for row in incidents],
    }
    result["quality_flags"] = list(
        dict.fromkeys(
            normal["quality_flags"]
            + result["quality_flags"]
            + ["conditional_on_reported_availability"]
            + (["unknown_outage_impact"] if unknown else [])
            + (
                ["outage_configuration_changed"]
                if any(row.declared_configuration_id != configuration.pk for row in incidents)
                else []
            )
            + (
                ["export_constraint_not_applied_to_ac_generation"]
                if configuration.measurement_basis == "ac_generation"
                and any(row.available_export_mw is not None for row in incidents)
                else []
            )
        )
    )
    return {
        "evaluated_at": known_at.isoformat(),
        "status": "unknown_impact" if unknown else "estimated",
        "incident_revision_ids": [row.revision_id for row in incidents],
        "incidents": [{**asdict(row), "id": row.incident_id} for row in incidents],
        "result": result,
    }
