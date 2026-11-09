from datetime import UTC, date, datetime, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pyracast.accounts.operations import require_member
from pyracast.connections.models import ProviderAccount
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
    snapshot: ForecastSnapshot, configuration: PlantConfiguration
) -> tuple[PowerInterval, ...]:
    result = []
    capacity = configuration.binding.capacity_kw
    if not isfinite(capacity) or capacity <= 0:
        raise ValueError("Invalid reference capacity")
    for row in snapshot.normalized_intervals:
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
            job__binding_id=configuration.binding_id,
            job__target_date=target_date,
            source_kind="forecast",
            collected_at__gte=prior_start,
            collected_at__lte=cutoff,
        )
        .filter(Q(issued_at__isnull=True) | Q(issued_at__lte=cutoff))
        .order_by("collected_at", "pk")
        .first()
    )


def _calibration(configuration: PlantConfiguration, as_of: datetime) -> CalibrationRun | None:
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
    ):
        return None
    return run


def _payload(
    configuration: PlantConfiguration,
    snapshot: ForecastSnapshot,
    calibration: CalibrationRun | None,
    target_date: date,
    resolution_minutes: int,
) -> dict[str, object]:
    start, end = local_day_bounds(target_date, configuration.binding.timezone)
    result = forecast(
        power_intervals(snapshot, configuration),
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
    if calibration is None:
        flags.append("uncalibrated")
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
        "selection_policy": SELECTION_POLICY,
        "snapshot_id": snapshot.pk,
        "binding_id": configuration.binding_id,
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
    region = Region.objects.get(pk=region_id, tenant_id=tenant_id)
    plant = Plant.objects.get(pk=plant_id, region=region)
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
        raise ValidationError("configuration_unavailable")
    # A historical binding may have a different timezone from the current region.
    configured_start, _ = local_day_bounds(target_date, configuration.binding.timezone)
    if as_of > configured_start:
        raise ValidationError("as_of must not follow the configured local day start.")
    calibration = _calibration(configuration, as_of)
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
        regional = ForecastResult.objects.filter(snapshot=snapshot).first()
        if regional is not None:
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
        "freshness": "missing"
        if snapshot is None
        else "fresh"
        if configuration_is_current(configuration)
        else "stale",
        "result": result,
        "regional_irradiance": irradiance,
    }
