from datetime import date, datetime
from typing import cast

from pyracast.forecasts.faults import FROZEN_PAYLOAD_FAULTS, stored_data
from pyracast.forecasts.history import composed_irradiance, live_state, result_state
from pyracast.forecasts.intraday import (
    ActualsMode,
    apply_intraday_source,
    prepare_intraday_source,
)
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.forecasts.sources import LaterFetch, current_forecast_snapshots
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_sources import SELECTION_POLICY, apply_composition, compose_snapshots
from pyracast.plants.forecasting import calibration_at, forecast_payload
from pyracast.plants.measurements import production_identity_current
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.plants.operations import configuration_is_current
from pyracast.plants.transfer import transfer_model


def current_forecast(
    plant: Plant,
    target_date: date,
    resolution_minutes: int,
    now: datetime,
    *,
    actuals: ActualsMode = "none",
    chosen: LaterFetch | None = None,
) -> dict[str, object]:
    """`chosen` answers a chosen fetch of today as it was known at its publication."""
    configuration = (
        PlantConfiguration.objects.select_related("binding__provider_account")
        .filter(pk=plant.current_configuration_id, plant=plant, created_at__lte=now)
        .first()
        if plant.current_configuration_id is not None
        else None
    )
    if configuration is not None and not configuration_is_current(configuration):
        configuration = None
    return _composed_forecast(
        plant,
        target_date,
        resolution_minutes,
        now,
        configuration,
        actuals=actuals,
        chosen=chosen,
    )


def _composed_forecast(
    plant: Plant,
    target_date: date,
    resolution_minutes: int,
    now: datetime,
    configuration: PlantConfiguration | None,
    *,
    actuals: ActualsMode = "none",
    chosen: LaterFetch | None = None,
) -> dict[str, object]:
    cutoff = chosen.published_at if chosen is not None else now
    actuals_only = actuals == "only"
    snapshot = None
    calibration = None
    result = None
    irradiance = None
    source_kind = "estimated_actuals" if actuals_only else "forecast"
    used_snapshots: list[ForecastSnapshot] = []
    if configuration is not None:
        start, end = local_day_bounds(target_date, configuration.binding.timezone)
        snapshots = current_forecast_snapshots(
            binding_id=configuration.binding_id,
            start=start,
            end=end,
            as_of=cutoff,
            pinned=chosen,
        )
        with stored_data("plant", plant.pk, target_date, "actuals", FROZEN_PAYLOAD_FAULTS):
            source = (
                prepare_intraday_source(
                    configuration.binding,
                    target_date,
                    snapshots,
                    knowledge_cutoff=cutoff,
                    elapsed_cutoff=cutoff,
                    actuals_only=actuals_only,
                )
                if actuals != "none"
                else None
            )
        if source is not None:
            snapshots = source.snapshots
            source_kind = source.source_kind
        elif actuals_only:
            snapshots = []
        snapshot = snapshots[0] if snapshots else None
        calibration = calibration_at(configuration, cutoff)
        if calibration is not None and not production_identity_current(calibration):
            calibration = None
        if snapshot is not None:
            with stored_data("plant", plant.pk, target_date, "forecast"):
                composition = (
                    source.composition
                    if source is not None
                    else compose_snapshots(snapshots, start, end)
                )
                used_snapshots = [row for row in snapshots if row.pk in composition.snapshot_ids]
                result = forecast_payload(
                    configuration,
                    snapshot,
                    calibration,
                    target_date,
                    resolution_minutes,
                    model_version=transfer_model(configuration, calibration),
                    source_intervals=composition.intervals,
                )
                if source is not None:
                    apply_intraday_source(result, source)
                else:
                    apply_composition(result, snapshots, composition)
                irradiance = composed_irradiance(
                    configuration.binding,
                    target_date,
                    snapshot=snapshot,
                    source_kind=source_kind,
                    snapshots=snapshots,
                    composition=composition,
                    source=source,
                )
            if source is None:
                cast(dict[str, object], result["provenance"])["selection_policy"] = SELECTION_POLICY
    return {
        "plant_id": plant.pk,
        "date": target_date.isoformat(),
        "timezone": configuration.binding.timezone if configuration else plant.region.timezone,
        "as_of": cutoff.isoformat(),
        "configuration_id": configuration.pk if configuration else None,
        "calibration_id": calibration.pk if calibration else None,
        "source_kind": source_kind,
        **result_state(
            snapshot is not None,
            live_state(
                connection_current=True,
                oldest_collected_at=min((row.collected_at for row in used_snapshots), default=None),
                now=now,
            ),
        ),
        "unavailable_reason": "configuration_unavailable"
        if configuration is None
        else "estimated_actuals_unavailable"
        if actuals_only and snapshot is None
        else "forecast_unavailable"
        if snapshot is None
        else None,
        "result": result,
        "regional_irradiance": irradiance,
    }
