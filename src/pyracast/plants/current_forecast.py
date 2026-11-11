from datetime import date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db.models import Q

from pyracast.calculation import calculate
from pyracast.connections.models import ProviderAccount
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.forecasts.sources import snapshot_data
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_sources import SELECTION_POLICY, apply_composition, compose_snapshots
from pyracast.plants.forecasting import _payload, calibration_at
from pyracast.plants.measurements import production_identity_current
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.plants.operations import configuration_is_current


def current_forecast(
    plant: Plant, target_date: date, resolution_minutes: int, now: datetime
) -> dict[str, object]:
    today = now.astimezone(ZoneInfo(plant.region.timezone)).date()
    if isinstance(resolution_minutes, bool) or resolution_minutes not in (30, 60):
        raise ValidationError("Resolution must be 30 or 60 minutes.")
    if not today <= target_date <= today + timedelta(days=14):
        raise ValidationError("date_out_of_range")
    configuration = (
        PlantConfiguration.objects.select_related("binding__provider_account")
        .filter(pk=plant.current_configuration_id, plant=plant, created_at__lte=now)
        .first()
        if plant.current_configuration_id is not None
        else None
    )
    if configuration is not None:
        # The outlook owns the surrounding transaction and region/plant locks.
        ProviderAccount.objects.select_for_update().get(
            pk=configuration.binding.provider_account_id
        )
        configuration.binding.refresh_from_db()
        configuration.binding.provider_account.refresh_from_db()
        if not configuration_is_current(configuration):
            configuration = None
    snapshot = None
    calibration = None
    result = None
    irradiance = None
    used_snapshots: list[ForecastSnapshot] = []
    if configuration is not None:
        start, end = local_day_bounds(target_date, configuration.binding.timezone)
        snapshots = list(
            ForecastSnapshot.objects.select_related("job")
            .filter(
                job__binding_id=configuration.binding_id,
                job__status="succeeded",
                source_kind="forecast",
                coverage_start__lt=end,
                coverage_end__gt=start,
                collected_at__lte=now,
                available_at__lte=now,
                forecastresult__published_at__lte=now,
            )
            .filter(Q(issued_at__isnull=True) | Q(issued_at__lte=now))
            .order_by("-collected_at", "-available_at", "-pk")
        )
        snapshot = snapshots[0] if snapshots else None
        calibration = calibration_at(configuration, now)
        if calibration is not None and not production_identity_current(calibration):
            calibration = None
        if snapshot is not None:
            try:
                composition = compose_snapshots(snapshots, start, end)
                used_snapshots = [
                    source for source in snapshots if source.pk in composition.snapshot_ids
                ]
                result = _payload(
                    configuration,
                    snapshot,
                    calibration,
                    target_date,
                    resolution_minutes,
                    source_intervals=composition.intervals,
                )
                apply_composition(result, snapshots, composition)
                irradiance = calculate(
                    configuration.binding,
                    target_date,
                    snapshot_data(snapshot, intervals=list(composition.intervals)),
                )
                irradiance["provenance"] = {
                    "snapshot_id": snapshot.pk,
                    "binding_id": configuration.binding_id,
                    "source_kind": "forecast",
                }
                cast(dict[str, object], irradiance["vintage"])["scope"] = "primary_forecast_source"
                apply_composition(irradiance, snapshots, composition)
            except (ValueError, ArithmeticError) as exc:
                raise ValidationError("calculation_out_of_range") from exc
            cast(dict[str, object], result["provenance"])["selection_policy"] = SELECTION_POLICY
            configuration.binding.refresh_from_db()
            configuration.binding.provider_account.refresh_from_db()
            if not configuration_is_current(configuration):
                configuration = None
                snapshot = None
                calibration = None
                result = None
                irradiance = None
    return {
        "plant_id": plant.pk,
        "date": target_date.isoformat(),
        "timezone": configuration.binding.timezone if configuration else plant.region.timezone,
        "as_of": now.isoformat(),
        "configuration_id": configuration.pk if configuration else None,
        "calibration_id": calibration.pk if calibration else None,
        "source_kind": "forecast",
        "freshness": "missing"
        if snapshot is None
        else "fresh"
        if all(source.collected_at >= now - timedelta(hours=24) for source in used_snapshots)
        else "stale",
        "unavailable_reason": "configuration_unavailable"
        if configuration is None
        else "forecast_unavailable"
        if snapshot is None
        else None,
        "result": result,
        "regional_irradiance": irradiance,
    }
