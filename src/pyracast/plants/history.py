from datetime import UTC, date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext

from pyracast.accounts.authorization import tenant_membership
from pyracast.db import read_snapshot
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
from pyracast.irradiance import local_day_bounds
from pyracast.plants.automatic_forecast import automatic_forecast
from pyracast.plants.current_forecast import current_forecast, saved_forecast
from pyracast.plants.forecasting import (
    archived_forecast,
    calibration_for_day,
    forecast_payload,
    plant_forecast,
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
    from pyracast.plants.incident_forecasts import incident_scenario
    from pyracast.plants.production_diagnostics import production_diagnostic

    with read_snapshot():
        tenant_membership(actor, tenant_id)
        region = Region.objects.active().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.get(pk=plant_id, region=region)
        plant.region = region
        result = _plant_outlook(
            actor,
            tenant_id,
            plant,
            target_date=target_date,
            resolution_minutes=resolution_minutes,
            as_of=as_of,
            source=source,
        )
        scenario_at = (
            datetime.fromisoformat(cast(str, result["as_of"]))
            if source == "archived_forecast"
            else as_of or timezone.now()
        )
        frozen = "outage_scenario" in result
        if not frozen:
            evaluated = incident_scenario(result, target_date, resolution_minutes, scenario_at)
            if evaluated.replacement_result is not None:
                result["result"] = evaluated.replacement_result
                result["calibration_id"] = None
            result["outage_scenario"] = evaluated.scenario
        result["production_diagnostic"] = (
            production_diagnostic(result, target_date, scenario_at)
            if as_of is None and not frozen
            else None
        )
        return result


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
    actor: User,
    tenant_id: int,
    plant: Plant,
    *,
    target_date: date,
    resolution_minutes: int,
    as_of: datetime | None,
    source: str,
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
    if source == "auto" and as_of is None and not ready:
        fallback = saved_forecast(plant, target_date, resolution_minutes, now)
        if fallback["result"] is not None:
            return fallback
    if actual_source is None and source == "auto" and as_of is not None:
        return plant_forecast(
            actor,
            tenant_id,
            plant.region.pk,
            plant.pk,
            target_date=target_date,
            resolution_minutes=resolution_minutes,
            as_of=cutoff,
        )
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


def _plant_outlook(
    actor: User,
    tenant_id: int,
    plant: Plant,
    *,
    target_date: date,
    resolution_minutes: int,
    as_of: datetime | None = None,
    source: str = "forecast",
) -> dict[str, object]:
    region = plant.region
    now = timezone.now()
    today = now.astimezone(ZoneInfo(region.timezone)).date()
    if source not in {"auto", "forecast", "estimated_actuals", "archived_forecast"}:
        raise ValidationError(gettext("Unsupported data source."))
    if source == "archived_forecast":
        return archived_forecast(plant, target_date, resolution_minutes, now, as_of=as_of)
    if source == "estimated_actuals" and target_date == today and as_of is None:
        return current_forecast(plant, target_date, resolution_minutes, now, actuals_only=True)
    if source == "forecast" or (source == "auto" and target_date >= today):
        if as_of is None and target_date >= today:
            selection = automatic_forecast if source == "auto" else current_forecast
            return selection(plant, target_date, resolution_minutes, now)
        return plant_forecast(
            actor,
            tenant_id,
            region.pk,
            plant.pk,
            target_date=target_date,
            resolution_minutes=resolution_minutes,
            as_of=as_of,
        )
    if isinstance(resolution_minutes, bool) or resolution_minutes not in (30, 60):
        raise ValidationError(gettext("Resolution must be 30 or 60 minutes."))
    if not today - timedelta(days=366) <= target_date < today:
        raise ValidationError(
            gettext("Estimated actuals require a completed local day within the past year.")
        )
    if as_of is not None and (as_of.utcoffset() is None or as_of > now):
        raise ValidationError(gettext("as_of must be aware and no later than now."))
    return _past_actuals(
        actor,
        tenant_id,
        plant,
        target_date=target_date,
        resolution_minutes=resolution_minutes,
        as_of=as_of,
        source=source,
        now=now,
    )
