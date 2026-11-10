from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone

from pyracast.accounts.operations import require_member
from pyracast.forecasts.history import historical_irradiance, select_actual_snapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecasting import _payload, plant_forecast
from pyracast.plants.models import Plant, PlantConfiguration
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
    require_member(actor, tenant_id)
    region = Region.objects.get(pk=region_id, tenant_id=tenant_id)
    plant = Plant.objects.get(pk=plant_id, region=region)
    now = timezone.now()
    today = now.astimezone(ZoneInfo(region.timezone)).date()
    if source not in {"auto", "forecast", "estimated_actuals"}:
        raise ValidationError("Unsupported data source.")
    if source == "forecast" or (source == "auto" and target_date >= today):
        return plant_forecast(
            actor,
            tenant_id,
            region_id,
            plant_id,
            target_date=target_date,
            resolution_minutes=resolution_minutes,
            as_of=as_of,
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
    configuration = (
        PlantConfiguration.objects.select_related("binding__provider_account")
        .filter(plant=plant, created_at__lte=cutoff)
        .order_by("-created_at", "-pk")
        .first()
    )
    snapshot = (
        select_actual_snapshot(configuration.binding, target_date, cutoff)
        if configuration is not None
        else None
    )
    if snapshot is None and source == "auto":
        return plant_forecast(
            actor,
            tenant_id,
            region_id,
            plant_id,
            target_date=target_date,
            resolution_minutes=resolution_minutes,
            as_of=min(cutoff, local_day_bounds(target_date, region.timezone)[0]),
        )
    result = None
    irradiance = None
    if snapshot is not None and configuration is not None:
        try:
            result = _payload(configuration, snapshot, None, target_date, resolution_minutes)
            irradiance = historical_irradiance(snapshot, target_date)
        except (ValueError, ArithmeticError) as exc:
            raise ValidationError("calculation_out_of_range") from exc
    return {
        "plant_id": plant.pk,
        "date": target_date.isoformat(),
        "timezone": configuration.binding.timezone if configuration else region.timezone,
        "as_of": cutoff.isoformat(),
        "configuration_id": configuration.pk if configuration else None,
        "calibration_id": None,
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
