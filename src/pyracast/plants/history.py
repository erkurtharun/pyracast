from datetime import UTC, date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from pyracast.accounts.operations import require_member
from pyracast.forecasts.history import historical_irradiance, select_actual_snapshot
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.automatic_forecast import automatic_forecast
from pyracast.plants.current_forecast import current_forecast
from pyracast.plants.forecasting import (
    archived_forecast,
    calibration_for_day,
    declared_forecast,
    forecast_payload,
    plant_forecast,
)
from pyracast.plants.measurements import production_identity_current
from pyracast.plants.models import CalibrationRun, Plant, PlantConfiguration
from pyracast.plants.operations import configuration_is_current
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

    require_member(actor, tenant_id)
    with transaction.atomic():
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        plant = Plant.objects.select_for_update().get(
            pk=plant_id, region=region, deleted_at__isnull=True
        )
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
        result["outage_scenario"] = incident_scenario(
            result, target_date, resolution_minutes, scenario_at
        )
        result["production_diagnostic"] = (
            production_diagnostic(result, target_date, scenario_at)
            if as_of is None and source != "archived_forecast"
            else None
        )
        return result


def _current_actuals(
    plant: Plant, target_date: date, cutoff: datetime
) -> tuple[PlantConfiguration, ForecastSnapshot, CalibrationRun | None] | None:
    """Prefer the current declaration for a past day that its binding archived, with the
    calibration whose training period covers that day; both are flagged in the result."""
    if plant.current_configuration_id is None:
        return None
    current = PlantConfiguration.objects.select_related("binding__provider_account").get(
        pk=plant.current_configuration_id, plant=plant
    )
    snapshot = select_actual_snapshot(current.binding, target_date, cutoff)
    if snapshot is None:
        return None
    calibration = calibration_for_day(current, target_date, cutoff)
    if calibration is not None and not production_identity_current(calibration):
        calibration = None
    return current, snapshot, calibration


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
    require_member(actor, tenant_id)
    region = plant.region
    now = timezone.now()
    today = now.astimezone(ZoneInfo(region.timezone)).date()
    if source not in {"auto", "forecast", "estimated_actuals", "archived_forecast"}:
        raise ValidationError("Unsupported data source.")
    if source == "archived_forecast":
        return archived_forecast(plant, target_date, resolution_minutes, now, as_of=as_of)
    if source == "forecast" or (source == "auto" and target_date >= today):
        if as_of is None and target_date >= today:
            selection = automatic_forecast if source == "auto" else current_forecast
            return selection(plant, target_date, resolution_minutes, now)
        return _forecast_outlook(
            actor,
            tenant_id,
            region.pk,
            plant,
            target_date=target_date,
            resolution_minutes=resolution_minutes,
            as_of=as_of,
            projection_at=now if as_of is None else None,
        )
    if isinstance(resolution_minutes, bool) or resolution_minutes not in (30, 60):
        raise ValidationError("Resolution must be 30 or 60 minutes.")
    if not today - timedelta(days=366) <= target_date < today:
        raise ValidationError(
            "Estimated actuals require a completed local day within the past year."
        )
    if as_of is not None and (as_of.utcoffset() is None or as_of > now):
        raise ValidationError("as_of must be aware and no later than now.")
    cutoff = (as_of or now).astimezone(UTC)
    configurations = PlantConfiguration.objects.select_related("binding__provider_account").filter(
        plant=plant, created_at__lte=cutoff
    )
    configuration: PlantConfiguration | None = None
    snapshot: ForecastSnapshot | None = None
    calibration: CalibrationRun | None = None
    selected = _current_actuals(plant, target_date, cutoff) if as_of is None else None
    if selected is not None:
        configuration, snapshot, calibration = selected
    else:
        if as_of is None:
            day_start, day_end = local_day_bounds(target_date, region.timezone)
            configuration = (
                configurations.filter(created_at__lte=day_start)
                .order_by("-created_at", "-pk")
                .first()
            )
            if configuration is None:
                configuration = (
                    configurations.filter(created_at__lt=day_end)
                    .order_by("-created_at", "-pk")
                    .first()
                )
        if configuration is None:
            ordering = ("created_at", "pk") if as_of is None else ("-created_at", "-pk")
            configuration = configurations.order_by(*ordering).first()
        if configuration is not None:
            snapshot = select_actual_snapshot(configuration.binding, target_date, cutoff)
    if snapshot is None and source == "auto" and as_of is not None:
        return _forecast_outlook(
            actor,
            tenant_id,
            region.pk,
            plant,
            target_date=target_date,
            resolution_minutes=resolution_minutes,
            as_of=min(cutoff, local_day_bounds(target_date, region.timezone)[0]),
            projection_at=now if as_of is None else None,
        )
    result = None
    irradiance = None
    if snapshot is not None and configuration is not None:
        try:
            result = forecast_payload(
                configuration, snapshot, calibration, target_date, resolution_minutes
            )
            irradiance = historical_irradiance(snapshot, target_date)
        except (ValueError, ArithmeticError) as exc:
            raise ValidationError("calculation_out_of_range") from exc
        if calibration is not None:
            cast(list[str], result["quality_flags"]).append("retrospective_calibration")
    return {
        "plant_id": plant.pk,
        "date": target_date.isoformat(),
        "timezone": configuration.binding.timezone if configuration else region.timezone,
        "as_of": cutoff.isoformat(),
        "configuration_id": configuration.pk if configuration else None,
        "calibration_id": calibration.pk if calibration else None,
        "source_kind": "estimated_actuals",
        "freshness": "missing"
        if snapshot is None
        else "fresh"
        if configuration is not None and configuration_is_current(configuration)
        else "stale",
        "unavailable_reason": "configuration_unavailable"
        if configuration is None
        else "estimated_actuals_unavailable"
        if snapshot is None
        else None,
        "result": result,
        "regional_irradiance": irradiance,
    }


def _forecast_outlook(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant: Plant,
    *,
    target_date: date,
    resolution_minutes: int,
    as_of: datetime | None,
    projection_at: datetime | None,
) -> dict[str, object]:
    result = plant_forecast(
        actor,
        tenant_id,
        region_id,
        plant.pk,
        target_date=target_date,
        resolution_minutes=resolution_minutes,
        as_of=as_of,
    )
    if projection_at is None or result["unavailable_reason"] != "configuration_unavailable":
        return result
    return (
        declared_forecast(
            plant,
            target_date,
            resolution_minutes,
            datetime.fromisoformat(cast(str, result["as_of"])),
            projection_at,
        )
        or result
    )
