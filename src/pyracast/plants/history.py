from datetime import UTC, date, datetime
from typing import cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext

from pyracast.accounts.authorization import tenant_membership
from pyracast.db import read_snapshot
from pyracast.errors import DomainError, ErrorCode
from pyracast.forecasts.actuals import (
    ArchivedActualSource,
    apply_archived_actual_source,
    archived_actual_irradiance,
    select_archived_actual_source,
)
from pyracast.forecasts.faults import stored_data
from pyracast.forecasts.history import (
    ARCHIVED,
    actuals_ready,
    historical_irradiance,
    result_state,
)
from pyracast.forecasts.outlook import (
    LIVE_MODES,
    Policy,
    is_source,
    plant_days_include,
    plant_policy,
)
from pyracast.irradiance import local_day_bounds
from pyracast.plants.automatic_forecast import automatic_forecast
from pyracast.plants.current_forecast import current_forecast, saved_forecast
from pyracast.plants.forecasting import (
    calibration_for_day,
    forecast_payload,
    issued_forecast,
    require_known_cutoff,
    require_resolution,
)
from pyracast.plants.measurements import production_identity_current
from pyracast.plants.models import CalibrationRun, Plant, PlantConfiguration
from pyracast.plants.transfer import transfer_model
from pyracast.regions.models import Region


def plant_outlook(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    target_date: date,
    resolution_minutes: int,
    as_of: datetime | None = None,
    source: str = "forecast",
) -> dict[str, object]:
    from pyracast.plants.production_diagnostics import production_diagnostic

    with read_snapshot():
        tenant_membership(actor, tenant_id)
        region = Region.objects.active().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.get(pk=plant_id, region=region)
        plant.region = region
        result = _plant_outlook(
            plant,
            target_date=target_date,
            resolution_minutes=resolution_minutes,
            as_of=as_of,
            source=source,
        )
        scenario_at = as_of or timezone.now()
        frozen = "outage_scenario" in result
        if not frozen:
            _evaluate_outage_scenario(result, target_date, resolution_minutes, scenario_at)
        result["production_diagnostic"] = (
            production_diagnostic(result, target_date, scenario_at)
            if as_of is None and not frozen
            else None
        )
        return result


def automatic_outlook(
    plant: Plant, target_date: date, resolution_minutes: int
) -> dict[str, object]:
    result = _plant_outlook(
        plant,
        target_date=target_date,
        resolution_minutes=resolution_minutes,
        as_of=None,
        source="auto",
    )
    _evaluate_outage_scenario(result, target_date, resolution_minutes, timezone.now())
    return result


def _evaluate_outage_scenario(
    result: dict[str, object], target_date: date, resolution_minutes: int, scenario_at: datetime
) -> None:
    from pyracast.plants.incident_forecasts import incident_scenario

    evaluated = incident_scenario(result, target_date, resolution_minutes, scenario_at)
    if evaluated.replacement_result is not None:
        result["result"] = evaluated.replacement_result
        result["calibration_id"] = None
    result["outage_scenario"] = evaluated.scenario


def _current_actuals(
    plant: Plant, target_date: date, cutoff: datetime
) -> tuple[PlantConfiguration, ArchivedActualSource, CalibrationRun | None] | None:
    """Prefer the current declaration for a past day that its binding archived, with the
    calibration whose training period covers that day; both are flagged in the result."""
    if plant.current_configuration_id is None:
        return None
    current = PlantConfiguration.objects.select_related("binding__provider_account").get(
        pk=plant.current_configuration_id, plant=plant
    )
    source = select_archived_actual_source(current.binding, target_date, cutoff)
    if source is None:
        return None
    calibration = calibration_for_day(current, target_date, cutoff)
    if calibration is not None and not production_identity_current(calibration):
        calibration = None
    return current, source, calibration


def _declared_actuals(
    plant: Plant, target_date: date, cutoff: datetime, as_of: datetime | None
) -> tuple[PlantConfiguration | None, ArchivedActualSource | None]:
    configurations = PlantConfiguration.objects.select_related("binding__provider_account").filter(
        plant=plant, created_at__lte=cutoff
    )
    configuration: PlantConfiguration | None = None
    if as_of is None:
        day_start, day_end = local_day_bounds(target_date, plant.region.timezone)
        configuration = (
            configurations.filter(created_at__lte=day_start).order_by("-created_at", "-pk").first()
            or configurations.filter(created_at__lt=day_end).order_by("-created_at", "-pk").first()
        )
    if configuration is None:
        ordering = ("created_at", "pk") if as_of is None else ("-created_at", "-pk")
        configuration = configurations.order_by(*ordering).first()
    if configuration is None:
        return None, None
    return configuration, select_archived_actual_source(configuration.binding, target_date, cutoff)


def _past_actuals(
    plant: Plant,
    *,
    target_date: date,
    resolution_minutes: int,
    as_of: datetime | None,
    fallback: bool,
    now: datetime,
) -> dict[str, object]:
    cutoff = (as_of or now).astimezone(UTC)
    configuration: PlantConfiguration | None
    actual_source: ArchivedActualSource | None
    calibration: CalibrationRun | None = None
    irradiance = None
    result = None
    with stored_data("plant", plant.pk, target_date, "estimated_actuals"):
        selected = _current_actuals(plant, target_date, cutoff) if as_of is None else None
        if selected is not None:
            configuration, actual_source, calibration = selected
        else:
            configuration, actual_source = _declared_actuals(plant, target_date, cutoff, as_of)
        if configuration is not None and actual_source is not None:
            irradiance = (
                archived_actual_irradiance(configuration.binding, target_date, actual_source)
                if actual_source.composition is not None
                else historical_irradiance(actual_source.snapshot, target_date)
            )
        ready = (
            actual_source is not None
            and irradiance is not None
            and actuals_ready(actual_source.snapshot, target_date, irradiance)
        )
    if fallback and as_of is None and not ready:
        saved = saved_forecast(plant, target_date, resolution_minutes, now)
        if saved["result"] is not None:
            return saved
    if fallback and actual_source is None and as_of is not None:
        return issued_forecast(plant, target_date, resolution_minutes, now, as_of=cutoff)
    if configuration is not None and actual_source is not None:
        with stored_data("plant", plant.pk, target_date, "estimated_actuals"):
            result = forecast_payload(
                configuration,
                actual_source.snapshot,
                calibration,
                target_date,
                resolution_minutes,
                model_version=transfer_model(configuration, calibration),
                source_intervals=actual_source.composition.intervals
                if actual_source.composition is not None
                else None,
            )
            apply_archived_actual_source(result, actual_source)
        flags = cast(list[str], result["quality_flags"])
        if calibration is not None:
            flags.append("retrospective_calibration")
        if configuration.pk != plant.current_configuration_id:
            flags.append("previous_configuration")
    return {
        "plant_id": plant.pk,
        "date": target_date.isoformat(),
        "timezone": configuration.binding.timezone if configuration else plant.region.timezone,
        "as_of": cutoff.isoformat(),
        "configuration_id": configuration.pk if configuration else None,
        "calibration_id": calibration.pk if calibration else None,
        "source_kind": "estimated_actuals",
        **result_state(actual_source is not None, ARCHIVED),
        "unavailable_reason": "configuration_unavailable"
        if configuration is None
        else "estimated_actuals_unavailable"
        if actual_source is None
        else None,
        "result": result,
        "regional_irradiance": irradiance,
    }


def _outside(policy: Policy) -> ValidationError:
    if policy in (Policy.ARCHIVED_ACTUALS, Policy.ACTUALS_THEN_SAVED_FORECAST):
        return ValidationError(
            gettext("Estimated actuals require a completed local day within the past year.")
        )
    return DomainError(ErrorCode.DATE_OUT_OF_RANGE)


def _plant_outlook(
    plant: Plant,
    *,
    target_date: date,
    resolution_minutes: int,
    as_of: datetime | None,
    source: str,
) -> dict[str, object]:
    if not is_source(source):
        raise ValidationError(gettext("Unsupported data source."))
    require_resolution(resolution_minutes)
    now = timezone.now()
    today = now.astimezone(ZoneInfo(plant.region.timezone)).date()
    policy = plant_policy(source, target_date, today, as_of=as_of is not None)
    if not plant_days_include(policy, target_date, today):
        raise _outside(policy)
    require_known_cutoff(as_of, now)
    match policy:
        case Policy.ARCHIVE | Policy.ISSUANCE:
            return issued_forecast(plant, target_date, resolution_minutes, now, as_of=as_of)
        case Policy.AUTOMATIC:
            return automatic_forecast(plant, target_date, resolution_minutes, now)
        case Policy.LIVE | Policy.LIVE_ACTUALS:
            return current_forecast(
                plant, target_date, resolution_minutes, now, actuals=LIVE_MODES[policy]
            )
    return _past_actuals(
        plant,
        target_date=target_date,
        resolution_minutes=resolution_minutes,
        as_of=as_of,
        fallback=policy is Policy.ACTUALS_THEN_SAVED_FORECAST,
        now=now,
    )
