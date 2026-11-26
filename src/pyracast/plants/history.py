from datetime import UTC, date, datetime
from typing import cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext

from pyracast.accounts.authorization import tenant_membership
from pyracast.db import read_snapshot
from pyracast.errors import UNEXPECTED, DomainError, ErrorCode
from pyracast.forecasts.actuals import (
    ArchivedActualSource,
    apply_archived_actual_source,
    archived_actual_irradiance,
    select_archived_actual_source,
)
from pyracast.forecasts.faults import stored_data
from pyracast.forecasts.history import ARCHIVED, historical_irradiance, result_state
from pyracast.forecasts.outlook import Policy, is_source, plant_days_include, plant_policy
from pyracast.forecasts.selection import day_of
from pyracast.irradiance import local_day_bounds
from pyracast.plants.current_forecast import current_forecast
from pyracast.plants.forecast_choice import forecast_outlook
from pyracast.plants.forecasting import calibration_for_day, forecast_payload, require_resolution
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
    source: str = "forecast",
    forecast_id: int | None = None,
) -> dict[str, object]:
    from pyracast.plants.production_diagnostics import production_diagnostic

    with read_snapshot():
        tenant_membership(actor, tenant_id)
        region = Region.objects.active().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.get(pk=plant_id, region=region)
        plant.region = region
        now = timezone.now()
        result = _plant_outlook(
            plant,
            target_date=target_date,
            resolution_minutes=resolution_minutes,
            source=source,
            forecast_id=forecast_id,
            now=now,
        )
        frozen = "outage_scenario" in result
        if not frozen:
            _evaluate_outage_scenario(result, target_date, resolution_minutes, now)
        result["production_diagnostic"] = None if frozen else production_diagnostic(result, now)
        return result


def automatic_outlook(
    plant: Plant, target_date: date, resolution_minutes: int, now: datetime
) -> dict[str, object]:
    result = _plant_outlook(
        plant,
        target_date=target_date,
        resolution_minutes=resolution_minutes,
        source="forecast",
        forecast_id=None,
        now=now,
    )
    if "outage_scenario" not in result:
        _evaluate_outage_scenario(result, target_date, resolution_minutes, now)
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
    plant: Plant, target_date: date, cutoff: datetime
) -> tuple[PlantConfiguration | None, ArchivedActualSource | None]:
    configurations = PlantConfiguration.objects.select_related("binding__provider_account").filter(
        plant=plant, created_at__lte=cutoff
    )
    day_start, day_end = local_day_bounds(target_date, plant.region.timezone)
    configuration = (
        configurations.filter(created_at__lte=day_start).order_by("-created_at", "-pk").first()
        or configurations.filter(created_at__lt=day_end).order_by("-created_at", "-pk").first()
        or configurations.order_by("created_at", "pk").first()
    )
    if configuration is None:
        return None, None
    return configuration, select_archived_actual_source(configuration.binding, target_date, cutoff)


def _past_actuals(
    plant: Plant, *, target_date: date, resolution_minutes: int, now: datetime
) -> dict[str, object]:
    cutoff = now.astimezone(UTC)
    configuration: PlantConfiguration | None
    actual_source: ArchivedActualSource | None
    calibration: CalibrationRun | None = None
    irradiance = None
    result = None
    with stored_data("plant", plant.pk, target_date, "estimated_actuals"):
        selected = _current_actuals(plant, target_date, cutoff)
        if selected is not None:
            configuration, actual_source, calibration = selected
        else:
            configuration, actual_source = _declared_actuals(plant, target_date, cutoff)
        if configuration is not None and actual_source is not None:
            irradiance = (
                archived_actual_irradiance(configuration.binding, target_date, actual_source)
                if actual_source.composition is not None
                else historical_irradiance(actual_source.snapshot, target_date)
            )
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
    if configuration is not None and result is not None:
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
    if policy is Policy.ARCHIVED_ACTUALS:
        return ValidationError(gettext("Choose a day before today and within the past year."))
    return DomainError(ErrorCode.DATE_OUT_OF_RANGE)


def _plant_outlook(
    plant: Plant,
    *,
    target_date: date,
    resolution_minutes: int,
    source: str,
    forecast_id: int | None,
    now: datetime,
) -> dict[str, object]:
    if not is_source(source):
        raise ValidationError(UNEXPECTED)
    require_resolution(resolution_minutes)
    today = now.astimezone(ZoneInfo(plant.region.timezone)).date()
    policy = plant_policy(source, target_date, today)
    if not plant_days_include(policy, target_date, today):
        raise _outside(policy)
    match policy:
        case Policy.LIVE_ACTUALS:
            answer = current_forecast(plant, target_date, resolution_minutes, now, actuals="only")
        case Policy.ARCHIVED_ACTUALS:
            answer = _past_actuals(
                plant, target_date=target_date, resolution_minutes=resolution_minutes, now=now
            )
        case _:
            return forecast_outlook(
                plant,
                target_date,
                resolution_minutes,
                day=day_of(target_date, today),
                policy=policy,
                forecast_id=forecast_id,
                now=now,
            )
    return answer | {"selection": None}
