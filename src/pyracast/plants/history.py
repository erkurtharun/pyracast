from datetime import UTC, date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from pyracast.accounts.operations import require_member
from pyracast.forecasts.actuals import (
    ArchivedActualSource,
    apply_archived_actual_source,
    archived_actual_irradiance,
    select_archived_actual_source,
)
from pyracast.forecasts.history import actuals_ready, historical_irradiance
from pyracast.forecasts.models import ForecastSnapshot
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
        frozen = "outage_scenario" in result
        if not frozen:
            result["outage_scenario"] = incident_scenario(
                result, target_date, resolution_minutes, scenario_at
            )
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
    cutoff = (as_of or now).astimezone(UTC)
    configurations = PlantConfiguration.objects.select_related("binding__provider_account").filter(
        plant=plant, created_at__lte=cutoff
    )
    configuration: PlantConfiguration | None = None
    snapshot: ForecastSnapshot | None = None
    actual_source: ArchivedActualSource | None = None
    calibration: CalibrationRun | None = None
    selected = _current_actuals(plant, target_date, cutoff) if as_of is None else None
    if selected is not None:
        configuration, actual_source, calibration = selected
        snapshot = actual_source.snapshot
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
            actual_source = select_archived_actual_source(
                configuration.binding, target_date, cutoff
            )
            snapshot = actual_source.snapshot if actual_source is not None else None
    irradiance = None
    if snapshot is not None:
        try:
            assert actual_source is not None  # noqa: S101 -- A selected snapshot has its pinned source.
            irradiance = (
                archived_actual_irradiance(configuration.binding, target_date, actual_source)
                if configuration is not None and actual_source.composition is not None
                else historical_irradiance(snapshot, target_date)
            )
        except (ValueError, ArithmeticError) as exc:
            raise ValidationError("calculation_out_of_range") from exc
    if (
        source == "auto"
        and as_of is None
        and (irradiance is None or not actuals_ready(snapshot, target_date, irradiance))
    ):
        fallback = saved_forecast(plant, target_date, resolution_minutes, now)
        if fallback["result"] is not None:
            return fallback
    if snapshot is None and source == "auto" and as_of is not None:
        return plant_forecast(
            actor,
            tenant_id,
            region.pk,
            plant.pk,
            target_date=target_date,
            resolution_minutes=resolution_minutes,
            as_of=cutoff,
        )
    result = None
    if snapshot is not None and configuration is not None:
        try:
            assert actual_source is not None  # noqa: S101 -- Snapshot selection pins its source.
            result = forecast_payload(
                configuration,
                snapshot,
                calibration,
                target_date,
                resolution_minutes,
                source_intervals=actual_source.composition.intervals
                if actual_source.composition is not None
                else None,
            )
            apply_archived_actual_source(result, actual_source)
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
