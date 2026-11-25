from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from pyracast.forecasts.intraday import (
    INTRADAY_POLICY,
    PreparedIntradaySource,
    apply_intraday_source,
)
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.forecasts.sources import mark_forecast_fallback, snapshot_rows
from pyracast.incidents.evidence import IncidentEvidence, select_incidents
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_sources import apply_composition, replay_composition
from pyracast.plants.forecasting import (
    forecast_daylight,
    forecast_payload,
    power_intervals,
    power_limit,
)
from pyracast.plants.incident_policy import incidents_unchanged
from pyracast.plants.models import CalibrationRun, PlantConfiguration
from pyracast.plants.numerics import forecast
from pyracast.plants.operating_limits import OperatingLimit, affected_power

SCENARIO_POLICY = "reported-operating-limits-v2"
CAPACITY_FIELDS = ("dc_capacity_mw", "inverter_capacity_mw", "export_limit_mw", "measurement_basis")


@dataclass(frozen=True)
class ScenarioResult:
    scenario: dict[str, object] | None
    replacement_result: dict[str, Any] | None


def declared_capacity_holds(
    incidents: Iterable[IncidentEvidence], configuration: PlantConfiguration
) -> dict[int, bool]:
    """Per revision id: whether the reported capacities still apply to `configuration`, that is
    the outage was declared under a configuration with the same capacities and meter basis."""
    rows = tuple(incidents)
    declared = PlantConfiguration.objects.filter(plant_id=configuration.plant_id).in_bulk(
        {row.declared_configuration_id for row in rows} - {configuration.pk}
    )
    declared[configuration.pk] = configuration
    return {
        row.revision_id: row.declared_configuration_id in declared
        and all(
            getattr(declared[row.declared_configuration_id], field) == getattr(configuration, field)
            for field in CAPACITY_FIELDS
        )
        for row in rows
    }


def operating_limits(
    incidents: Iterable[IncidentEvidence], holds: Mapping[int, bool]
) -> tuple[OperatingLimit, ...]:
    return tuple(
        OperatingLimit(
            row.starts_at,
            row.ends_at,
            row.available_dc_mw if holds[row.revision_id] else None,
            row.available_inverter_mw if holds[row.revision_id] else None,
            row.available_export_mw if holds[row.revision_id] else None,
        )
        for row in incidents
    )


def _limited_forecast(
    normal: dict[str, Any],
    configuration: PlantConfiguration,
    snapshot: ForecastSnapshot,
    source_intervals: tuple[dict[str, Any], ...] | None,
    limits: tuple[OperatingLimit, ...],
    bounds: tuple[datetime, datetime],
    resolution_minutes: int,
) -> dict[str, Any]:
    daylight, energy_windows = forecast_daylight(
        configuration,
        bounds[0].astimezone(ZoneInfo(configuration.binding.timezone)).date(),
        snapshot_rows(snapshot) if source_intervals is None else source_intervals,
    )
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
        daylight=daylight,
        energy_windows=energy_windows,
    )
    for key in ("factor", "limit_mw", "model_version", "configuration"):
        result[key] = normal[key]
    if source_intervals is not None:
        source_ids = {row["start"]: row["source_snapshot_ids"] for row in normal["intervals"]}
        for row in result["intervals"]:
            row["source_snapshot_ids"] = source_ids[row["start"]]
        source_kinds = {row["start"]: row.get("source_kinds") for row in normal["intervals"]}
        for row in result["intervals"]:
            if source_kinds[row["start"]] is not None:
                row["source_kinds"] = source_kinds[row["start"]]
    return result


def _uncalibrated(
    normal: dict[str, Any],
    configuration: PlantConfiguration,
    snapshot: ForecastSnapshot,
    selected_sources: Any,
    target_date: date,
    resolution_minutes: int,
) -> dict[str, Any]:
    provenance = normal["provenance"]
    result = cast(
        dict[str, Any],
        forecast_payload(
            configuration,
            snapshot,
            None,
            target_date,
            resolution_minutes,
            model_version=normal["model_version"],
            source_intervals=selected_sources[0].intervals if selected_sources else None,
        ),
    )
    if selected_sources is not None:
        if provenance.get("selection_policy") in (
            INTRADAY_POLICY,
            "native-estimated-actuals-coverage-v1",
        ):
            apply_intraday_source(
                result,
                PreparedIntradaySource(
                    snapshot,
                    selected_sources[1],
                    selected_sources[0],
                    provenance["source_kind"],
                    datetime.fromisoformat(provenance["actuals_cutoff_at"]),
                    datetime.fromisoformat(provenance["elapsed_cutoff_at"]),
                    datetime.fromisoformat(provenance["actuals_grace_until"])
                    if "actuals_grace_until" in provenance
                    else None,
                ),
            )
            result["provenance"]["selection_policy"] = provenance["selection_policy"]
        else:
            apply_composition(result, selected_sources[1], selected_sources[0])
    if provenance.get("fallback_reason") == "estimated_actuals_unavailable":
        mark_forecast_fallback(result, datetime.fromisoformat(provenance["selection_cutoff"]))
    result["quality_flags"].append("calibration_invalidated_by_outage_history")
    return result


def incident_scenario(
    outlook: Mapping[str, Any], target_date: date, resolution_minutes: int, known_at: datetime
) -> ScenarioResult:
    """The outage scenario of an outlook, and its replacement result when outage history
    changed after the outlook's calibration was fitted; the outlook itself is not changed."""
    normal: dict[str, Any] | None = outlook.get("result")
    if normal is None:
        return ScenarioResult(None, None)
    configuration = PlantConfiguration.objects.select_related("binding").get(
        pk=outlook["configuration_id"], plant_id=outlook["plant_id"]
    )
    start, end = local_day_bounds(target_date, configuration.binding.timezone)
    invalidated = False
    if outlook["calibration_id"] is not None:
        calibration = CalibrationRun.objects.get(
            pk=outlook["calibration_id"], configuration=configuration
        )
        invalidated = not incidents_unchanged(calibration, known_at)
    incidents = select_incidents(configuration.plant_id, start, end, known_at)
    scenario_needed = bool(incidents) and (
        known_at >= end
        or any(
            row.starts_at <= known_at and (row.ends_at is None or row.ends_at > known_at)
            for row in incidents
        )
    )
    if not invalidated and not scenario_needed:
        return ScenarioResult(None, None)
    snapshot = ForecastSnapshot.objects.defer("raw_payload").get(
        pk=normal["provenance"]["snapshot_id"], job__binding_id=configuration.binding_id
    )
    selected_sources = replay_composition(normal["provenance"], configuration, start, end)
    replacement = (
        _uncalibrated(
            normal, configuration, snapshot, selected_sources, target_date, resolution_minutes
        )
        if invalidated
        else None
    )
    if not scenario_needed:
        return ScenarioResult(None, replacement)
    base = replacement or normal
    holds = declared_capacity_holds(incidents, configuration)
    result = _limited_forecast(
        base,
        configuration,
        snapshot,
        selected_sources[0].intervals if selected_sources is not None else None,
        operating_limits(incidents, holds),
        (start, end),
        resolution_minutes,
    )
    changed = not all(holds.values())
    unknown = changed or any(
        row.available_dc_mw is None
        and row.available_inverter_mw is None
        and row.available_export_mw is None
        for row in incidents
    )
    result["provenance"] = {
        **base["provenance"],
        "scenario_policy": SCENARIO_POLICY,
        "evaluated_at": known_at.isoformat(),
        "incident_revision_ids": [row.revision_id for row in incidents],
    }
    result["quality_flags"] = list(
        dict.fromkeys(
            base["quality_flags"]
            + result["quality_flags"]
            + ["conditional_on_reported_availability"]
            + (["unknown_outage_impact"] if unknown else [])
            + (["outage_configuration_changed"] if changed else [])
            + (
                ["export_constraint_not_applied_to_ac_generation"]
                if configuration.measurement_basis == "ac_generation"
                and any(row.available_export_mw is not None for row in incidents)
                else []
            )
        )
    )
    scenario: dict[str, object] = {
        "evaluated_at": known_at.isoformat(),
        "status": "unknown_impact" if unknown else "estimated",
        "incident_revision_ids": [row.revision_id for row in incidents],
        "incidents": [{**asdict(row), "id": row.incident_id} for row in incidents],
        "result": result,
    }
    return ScenarioResult(scenario, replacement)
