from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import Any, cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pyracast.accounts.operations import require_member
from pyracast.connections.models import ProviderAccount
from pyracast.forecasts.capacity_normalization import normalization_provenance
from pyracast.forecasts.history import HISTORY_SELECTION_POLICY
from pyracast.forecasts.models import ForecastResult, ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantForecast,
)
from pyracast.plants.numerics import PowerInterval, forecast
from pyracast.plants.operations import configuration_is_current
from pyracast.regions.models import Region

SELECTION_POLICY = "first-available-day-ahead-v1"


def power_limit(configuration: PlantConfiguration) -> float | None:
    limits = [configuration.inverter_capacity_mw]
    if configuration.measurement_basis == "net_export":
        limits.append(configuration.export_limit_mw)
    known = [value for value in limits if value is not None]
    return min(known) if known else None


def power_intervals(
    snapshot: ForecastSnapshot,
    configuration: PlantConfiguration,
    *,
    source_intervals: tuple[dict[str, Any], ...] | None = None,
) -> tuple[PowerInterval, ...]:
    result = []
    capacity = configuration.binding.capacity_kw
    if not isfinite(capacity) or capacity <= 0:
        raise ValueError("Invalid reference capacity")
    for row in snapshot.normalized_intervals if source_intervals is None else source_intervals:
        start, end, power = row.get("start"), row.get("end"), row.get("power_kw")
        if not isinstance(start, str) or not isinstance(end, str):
            raise ValueError("Invalid source interval timestamps")
        if power is not None and (
            isinstance(power, bool)
            or not isinstance(power, int | float)
            or not isfinite(power)
            or power < 0
        ):
            raise ValueError("Invalid source power")
        result.append(
            PowerInterval(
                datetime.fromisoformat(start),
                datetime.fromisoformat(end),
                None if power is None else power / capacity * configuration.dc_capacity_mw,
            )
        )
    return tuple(result)


def select_snapshot(
    configuration: PlantConfiguration, target_date: date, as_of: datetime
) -> ForecastSnapshot | None:
    prior_start, day_start = local_day_bounds(
        target_date - timedelta(days=1), configuration.binding.timezone
    )
    cutoff = min(as_of, day_start)
    return (
        ForecastSnapshot.objects.filter(
            binding_id=configuration.binding_id,
            job__target_date=target_date,
            job__status="succeeded",
            source_kind="forecast",
            collected_at__gte=prior_start,
            collected_at__lte=cutoff,
            collected_at__lt=day_start,
            available_at__lte=cutoff,
            available_at__lt=day_start,
            forecastresult__published_at__lte=cutoff,
            forecastresult__published_at__lt=day_start,
        )
        .filter(Q(issued_at__isnull=True) | Q(issued_at__lte=cutoff, issued_at__lt=day_start))
        .order_by("collected_at", "pk")
        .first()
    )


def calibration_at(configuration: PlantConfiguration, as_of: datetime) -> CalibrationRun | None:
    from pyracast.plants.incident_policy import incidents_unchanged

    activation = (
        CalibrationActivation.objects.select_related("calibration")
        .filter(configuration=configuration, created_at__lte=as_of)
        .order_by("-created_at", "-pk")
        .first()
    )
    if activation is None or activation.calibration is None:
        return None
    run = activation.calibration
    if (
        run.configuration_id != configuration.pk
        or run.binding_id != configuration.binding_id
        or run.plant_id != configuration.plant_id
        or run.status != "ready"
        or run.factor is None
        or run.completed_at is None
        or run.completed_at > as_of
        or run.cutoff_at > as_of
        or run.created_at > as_of
        or not incidents_unchanged(run, as_of)
    ):
        return None
    return run


def _payload(
    configuration: PlantConfiguration,
    snapshot: ForecastSnapshot,
    calibration: CalibrationRun | None,
    target_date: date,
    resolution_minutes: int,
    *,
    source_intervals: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, object]:
    start, end = local_day_bounds(target_date, configuration.binding.timezone)
    result = forecast(
        power_intervals(snapshot, configuration, source_intervals=source_intervals),
        start=start,
        end=end,
        resolution_minutes=resolution_minutes,
        limit_mw=power_limit(configuration),
        factor=calibration.factor
        if calibration is not None and calibration.factor is not None
        else 1,
    )
    flags = list(result["quality_flags"]) if isinstance(result["quality_flags"], list) else []
    flags.extend(("unvalidated_reference_transfer", "geometry_not_modeled"))
    if snapshot.source_kind == "estimated_actuals":
        flags.append("estimated_actuals_not_metered")
        if configuration.created_at > start:
            flags.append("historical_configuration_assumed")
    if calibration is None:
        flags.append("uncalibrated")
    elif calibration.result.get("provisional_production"):
        flags.append("provisional_production_calibration")
    if power_limit(configuration) is None:
        flags.append("output_limit_unknown")
    result["quality_flags"] = flags
    result["configuration"] = {
        "id": configuration.pk,
        "revision": configuration.revision,
        "dc_capacity_mw": configuration.dc_capacity_mw,
        "inverter_capacity_mw": configuration.inverter_capacity_mw,
        "export_limit_mw": configuration.export_limit_mw,
        "measurement_basis": configuration.measurement_basis,
    }
    result["provenance"] = {
        "selection_policy": HISTORY_SELECTION_POLICY
        if snapshot.source_kind == "estimated_actuals"
        else SELECTION_POLICY,
        "snapshot_id": snapshot.pk,
        "binding_id": configuration.binding_id,
        "capacity_normalization": normalization_provenance(snapshot),
        "source_kind": snapshot.source_kind,
        "collected_at": snapshot.collected_at.isoformat(),
        "scheduled_slot": snapshot.job.scheduled_slot.isoformat(),
        "late": snapshot.collected_at > snapshot.job.scheduled_slot,
        "lead_hours": (start - snapshot.collected_at).total_seconds() / 3600,
        "issued_at": snapshot.issued_at.isoformat() if snapshot.issued_at else None,
        "calibration_id": calibration.pk if calibration else None,
        "calibration_assessment": calibration.assessment if calibration else "uncalibrated",
        "calibration_cutoff_at": calibration.cutoff_at.isoformat() if calibration else None,
        "calibration_dataset_sha256": calibration.dataset_sha256 if calibration else None,
    }
    return result


def archived_forecast(
    plant: Plant,
    target_date: date,
    resolution_minutes: int,
    now: datetime,
    as_of: datetime | None = None,
) -> dict[str, object]:
    region = plant.region
    today = now.astimezone(ZoneInfo(region.timezone)).date()
    if isinstance(resolution_minutes, bool) or resolution_minutes not in (30, 60):
        raise ValidationError("Resolution must be 30 or 60 minutes.")
    if not today - timedelta(days=366) <= target_date <= today:
        raise ValidationError("date_out_of_range")
    if as_of is not None and (as_of.utcoffset() is None or as_of > now):
        raise ValidationError("as_of must be aware and no later than now.")
    day_start, _ = local_day_bounds(target_date, region.timezone)
    cutoff = min(as_of or now, day_start).astimezone(UTC)
    configurations = PlantConfiguration.objects.select_related("binding__provider_account").filter(
        plant=plant, created_at__lte=cutoff
    )
    configuration = configurations.order_by("-created_at", "-pk").first()
    assumed = False
    if configuration is None and as_of is None:
        configuration = (
            PlantConfiguration.objects.select_related("binding__provider_account")
            .filter(plant=plant, created_at__lte=now)
            .order_by("created_at", "pk")
            .first()
        )
        assumed = configuration is not None
    if configuration is None:
        return {
            "plant_id": plant.pk,
            "date": target_date.isoformat(),
            "timezone": region.timezone,
            "as_of": cutoff.isoformat(),
            "configuration_id": None,
            "snapshot_id": None,
            "calibration_id": None,
            "source_kind": "forecast",
            "freshness": "missing",
            "unavailable_reason": "configuration_unavailable",
            "result": None,
            "regional_irradiance": None,
        }
    configured_start, _ = local_day_bounds(target_date, configuration.binding.timezone)
    cutoff = min(cutoff, configured_start)
    snapshot = select_snapshot(configuration, target_date, cutoff)
    calibration = (
        None
        if assumed
        else calibration_at(
            configuration, min(cutoff, configured_start - timedelta(microseconds=1))
        )
    )
    result: dict[str, object] | None = None
    irradiance: dict[str, object] | None = None
    if snapshot is not None:
        saved = PlantForecast.objects.filter(
            configuration=configuration,
            snapshot=snapshot,
            calibration=calibration,
            resolution_minutes=resolution_minutes,
            target_date=target_date,
        ).first()
        try:
            result = (
                deepcopy(saved.payload)
                if saved is not None and not assumed
                else _payload(configuration, snapshot, calibration, target_date, resolution_minutes)
            )
        except (ValueError, ArithmeticError) as exc:
            raise ValidationError("calculation_out_of_range") from exc
        flags = cast(list[str], result["quality_flags"])
        flags.append("archived_forecast")
        if assumed:
            flags.append("historical_configuration_assumed")
            cast(dict[str, object], result["provenance"])["configuration_created_at"] = (
                configuration.created_at.isoformat()
            )
        if configuration.pk != plant.current_configuration_id:
            flags.append("previous_configuration")
        regional = ForecastResult.objects.get(snapshot=snapshot)
        irradiance = {
            key: regional.payload.get(key)
            for key in ("energy_kwh_m2", "partial_energy_kwh_m2", "complete", "quality_flags")
        }
    return {
        "plant_id": plant.pk,
        "date": target_date.isoformat(),
        "timezone": configuration.binding.timezone,
        "as_of": cutoff.isoformat(),
        "configuration_id": configuration.pk,
        "snapshot_id": snapshot.pk if snapshot else None,
        "calibration_id": calibration.pk if calibration else None,
        "source_kind": "forecast",
        "freshness": "missing"
        if snapshot is None
        else "fresh"
        if configuration.pk == plant.current_configuration_id
        else "stale",
        "unavailable_reason": "forecast_unavailable" if snapshot is None else None,
        "result": result,
        "regional_irradiance": irradiance,
    }


def declared_forecast(
    plant: Plant,
    target_date: date,
    resolution_minutes: int,
    cutoff: datetime,
    now: datetime,
) -> dict[str, object] | None:
    if plant.current_configuration_id is None:
        return None
    configuration = (
        PlantConfiguration.objects.select_related("binding__provider_account")
        .filter(pk=plant.current_configuration_id, plant=plant, created_at__lte=now)
        .first()
    )
    if configuration is None or not configuration_is_current(configuration):
        return None
    snapshot = select_snapshot(configuration, target_date, cutoff)
    result = None
    irradiance = None
    if snapshot is not None:
        try:
            result = _payload(configuration, snapshot, None, target_date, resolution_minutes)
        except (ValueError, ArithmeticError) as exc:
            raise ValidationError("calculation_out_of_range") from exc
        cast(list[str], result["quality_flags"]).append("historical_configuration_assumed")
        cast(dict[str, object], result["provenance"])["configuration_created_at"] = (
            configuration.created_at.isoformat()
        )
        regional = ForecastResult.objects.get(snapshot=snapshot)
        irradiance = {
            key: regional.payload.get(key)
            for key in ("energy_kwh_m2", "partial_energy_kwh_m2", "complete", "quality_flags")
        }
    configuration.binding.refresh_from_db()
    configuration.binding.provider_account.refresh_from_db()
    if not configuration_is_current(configuration):
        return None
    return {
        "plant_id": plant.pk,
        "date": target_date.isoformat(),
        "timezone": configuration.binding.timezone,
        "as_of": cutoff.isoformat(),
        "configuration_id": configuration.pk,
        "calibration_id": None,
        "source_kind": "forecast",
        "freshness": "fresh" if snapshot is not None else "missing",
        "unavailable_reason": None if snapshot is not None else "forecast_unavailable",
        "result": result,
        "regional_irradiance": irradiance,
    }


def plant_forecast(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    target_date: date,
    resolution_minutes: int,
    as_of: datetime | None = None,
) -> dict[str, object]:
    require_member(actor, tenant_id)
    region = Region.objects.get(pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True)
    plant = Plant.objects.get(pk=plant_id, region=region, deleted_at__isnull=True)
    now = timezone.now()
    today = now.astimezone(ZoneInfo(region.timezone)).date()
    if isinstance(resolution_minutes, bool) or resolution_minutes not in (30, 60):
        raise ValidationError("Resolution must be 30 or 60 minutes.")
    if not today - timedelta(days=366) <= target_date <= today + timedelta(days=14):
        raise ValidationError("date_out_of_range")
    day_start, _ = local_day_bounds(target_date, region.timezone)
    if as_of is None:
        as_of = min(now, day_start)
    elif as_of.utcoffset() is None or as_of > now or as_of > day_start:
        raise ValidationError("as_of must be aware and no later than now or the target day start.")
    as_of = as_of.astimezone(UTC)
    configuration = (
        PlantConfiguration.objects.select_related("binding__provider_account")
        .filter(plant=plant, created_at__lte=as_of)
        .order_by("-created_at", "-pk")
        .first()
    )
    if configuration is None:
        return {
            "plant_id": plant.pk,
            "date": target_date.isoformat(),
            "timezone": region.timezone,
            "as_of": as_of.isoformat(),
            "configuration_id": None,
            "calibration_id": None,
            "freshness": "missing",
            "source_kind": "forecast",
            "unavailable_reason": "configuration_unavailable",
            "result": None,
            "regional_irradiance": None,
        }
    # A historical binding may have a different timezone from the current region.
    configured_start, _ = local_day_bounds(target_date, configuration.binding.timezone)
    if as_of > configured_start:
        raise ValidationError("as_of must not follow the configured local day start.")
    calibration = calibration_at(configuration, as_of)
    snapshot = select_snapshot(configuration, target_date, as_of)
    result: dict[str, object] | None = None
    irradiance: dict[str, object] | None = None
    if snapshot is not None:
        saved = PlantForecast.objects.filter(
            configuration=configuration,
            snapshot=snapshot,
            calibration=calibration,
            resolution_minutes=resolution_minutes,
            target_date=target_date,
        ).first()
        if saved is None:
            if not configuration_is_current(configuration):
                raise ValidationError("configuration_changed")
            try:
                payload = _payload(
                    configuration, snapshot, calibration, target_date, resolution_minutes
                )
            except (ValueError, ArithmeticError) as exc:
                raise ValidationError("calculation_out_of_range") from exc
            with transaction.atomic():
                Region.objects.select_for_update().get(pk=region.pk)
                Plant.objects.select_for_update().get(pk=plant.pk)
                ProviderAccount.objects.select_for_update().get(
                    pk=configuration.binding.provider_account_id
                )
                configuration.binding.refresh_from_db()
                configuration.binding.provider_account.refresh_from_db()
                if not configuration_is_current(configuration):
                    raise ValidationError("configuration_changed")
                saved, _ = PlantForecast.objects.get_or_create(
                    configuration=configuration,
                    snapshot=snapshot,
                    calibration=calibration,
                    resolution_minutes=resolution_minutes,
                    target_date=target_date,
                    defaults={"payload": payload},
                )
        result = saved.payload
        regional = ForecastResult.objects.get(snapshot=snapshot)
        irradiance = {
            key: regional.payload.get(key)
            for key in ("energy_kwh_m2", "partial_energy_kwh_m2", "complete", "quality_flags")
        }
    return {
        "plant_id": plant.pk,
        "date": target_date.isoformat(),
        "timezone": configuration.binding.timezone,
        "as_of": as_of.isoformat(),
        "configuration_id": configuration.pk,
        "calibration_id": calibration.pk if calibration else None,
        "source_kind": "forecast",
        "freshness": "missing"
        if snapshot is None
        else "fresh"
        if configuration_is_current(configuration)
        else "stale",
        "unavailable_reason": "forecast_unavailable" if snapshot is None else None,
        "result": result,
        "regional_irradiance": irradiance,
    }
