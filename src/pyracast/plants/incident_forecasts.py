from dataclasses import asdict
from datetime import date, datetime
from typing import Any, cast

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.incidents.operations import select_incidents
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_sources import apply_composition, replay_composition
from pyracast.plants.forecasting import _payload, power_intervals, power_limit
from pyracast.plants.incident_policy import incidents_unchanged
from pyracast.plants.models import CalibrationRun, PlantConfiguration
from pyracast.plants.numerics import forecast
from pyracast.plants.operating_limits import OperatingLimit, affected_power


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
                _payload(
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
    result = forecast(
        affected_power(
            power_intervals(snapshot, configuration, source_intervals=source_intervals),
            limits,
            dc_capacity_mw=configuration.dc_capacity_mw,
            normal_limit_mw=power_limit(configuration),
            factor=normal["factor"],
            net_export=configuration.measurement_basis == "net_export",
        ),
        start=start,
        end=end,
        resolution_minutes=resolution_minutes,
        limit_mw=None,
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
    result["factor"] = normal["factor"]
    result["limit_mw"] = normal["limit_mw"]
    result["configuration"] = normal["configuration"]
    if selected_sources is not None:
        source_ids = {row["start"]: row["source_snapshot_ids"] for row in normal["intervals"]}
        for row in cast(list[dict[str, Any]], result["intervals"]):
            row["source_snapshot_ids"] = source_ids[row["start"]]
    result["provenance"] = {
        **normal["provenance"],
        "scenario_policy": "reported-operating-limits-v1",
        "evaluated_at": known_at.isoformat(),
        "incident_revision_ids": [row.revision_id for row in incidents],
    }
    result["quality_flags"] = list(
        dict.fromkeys(
            normal["quality_flags"]
            + cast(list[str], result["quality_flags"])
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
