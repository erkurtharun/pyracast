from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import F, Q

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_archive import forecast_cutoff, select_forecast_issuance
from pyracast.plants.forecasting import calibration_at, forecast_payload
from pyracast.plants.measurements import production_identity_current
from pyracast.plants.models import (
    CalibrationRun,
    ForecastReconstruction,
    Plant,
    PlantConfiguration,
    PlantEvent,
)
from pyracast.regions.models import Region

RECONSTRUCTION_POLICY = "latest-native-pre-daylight-current-calibration-v1"


def _source(
    configuration: PlantConfiguration, day: date, reconstructed_at: datetime
) -> ForecastSnapshot | None:
    cutoff = forecast_cutoff(day, configuration.binding)
    start, end = local_day_bounds(day, configuration.binding.timezone)
    sources = (
        ForecastSnapshot.objects.select_related("job", "binding__provider_account")
        .defer("raw_payload", "weather_payload")
        .filter(
            binding_id=configuration.binding_id,
            job__binding_id=configuration.binding_id,
            job__status="succeeded",
            job__endpoint="forecasts",
            job__reference_import__isnull=True,
            capacity_normalization__isnull=True,
            source_kind="forecast",
            collected_at__gte=configuration.created_at,
            collected_at__lt=cutoff,
            available_at__gte=F("collected_at"),
            available_at__lt=cutoff,
            available_at__lte=reconstructed_at,
            coverage_start__lt=end,
            coverage_end__gt=start,
        )
        .filter(Q(issued_at__isnull=True) | Q(issued_at__lte=F("collected_at")))
        .order_by("-collected_at", "-available_at", "-pk")
    )
    for source in sources:
        first = source.collected_at.astimezone(ZoneInfo(configuration.binding.timezone)).date()
        if not first <= day <= first + timedelta(days=2):
            continue
        if any(
            row.get("source_kind", "forecast") != "forecast"
            or row.get("source_kinds") not in (None, [])
            for row in source.normalized_intervals
        ):
            continue
        return source
    return None


def _reconstruct_day(
    plant: Plant,
    configuration: PlantConfiguration,
    calibration: CalibrationRun,
    day: date,
    reconstructed_at: datetime,
    *,
    apply: bool,
) -> dict[str, object]:
    outcome: dict[str, object] = {"plant_id": plant.pk, "date": day.isoformat()}
    if select_forecast_issuance(plant, day, as_of=reconstructed_at) is not None:
        return {**outcome, "status": "issuance_exists"}
    source = _source(configuration, day, reconstructed_at)
    if source is None:
        return {**outcome, "status": "source_unavailable"}
    payloads = {
        str(resolution): forecast_payload(configuration, source, calibration, day, resolution)
        for resolution in (30, 60)
    }
    if not cast(dict[str, object], payloads["30"]["daylight"])["complete"]:
        return {**outcome, "status": "incomplete_source", "snapshot_id": source.pk}
    cutoff = forecast_cutoff(day, configuration.binding)
    for payload in payloads.values():
        cast(list[str], payload["quality_flags"]).append("reconstructed_forecast")
        cast(dict[str, object], payload["provenance"]).update(
            selection_policy=RECONSTRUCTION_POLICY,
            reconstructed_at=reconstructed_at.isoformat(),
            available_at=cast(datetime, source.available_at).isoformat(),
            selection_cutoff=cutoff.isoformat(),
            model_version=payload["model_version"],
            factor=calibration.factor,
        )
    identity = {
        "snapshot_id": source.pk,
        "configuration_id": configuration.pk,
        "calibration_id": calibration.pk,
        "target_date": day,
        "model_version": cast(str, payloads["30"]["model_version"]),
    }
    outcome.update(
        snapshot_id=source.pk, configuration_id=configuration.pk, calibration_id=calibration.pk
    )
    existing = ForecastReconstruction.objects.filter(**identity).first()
    if existing is not None:
        return {**outcome, "status": "already_exists", "reconstruction_id": existing.pk}
    if not apply:
        return {**outcome, "status": "preview"}
    reconstruction = ForecastReconstruction.objects.create(
        **identity,
        plant=plant,
        reconstructed_at=reconstructed_at,
        source_cutoff_at=cutoff,
        factor=cast(float, calibration.factor),
        payloads=payloads,
    )
    PlantEvent.objects.create(
        plant=plant,
        action="forecast_reconstructed",
        details={
            **outcome,
            "reconstruction_id": reconstruction.pk,
            "reconstructed_at": reconstructed_at.isoformat(),
        },
    )
    return {**outcome, "status": "created", "reconstruction_id": reconstruction.pk}


def reconstruct_forecasts(
    plant_ids: Sequence[int],
    start_date: date,
    end_date: date,
    reconstructed_at: datetime,
    *,
    apply: bool = False,
) -> list[dict[str, object]]:
    if reconstructed_at.utcoffset() is None or end_date < start_date:
        raise ValueError("An aware reconstruction time and ordered dates are required.")
    identifiers = set(plant_ids)
    if not identifiers:
        raise ValueError("At least one plant is required.")
    with transaction.atomic():
        region_ids = Plant.objects.filter(pk__in=identifiers).values("region_id")
        list(Region.objects.select_for_update().filter(pk__in=region_ids).order_by("pk"))
        plants = list(
            Plant.objects.select_for_update()
            .select_related("region")
            .filter(pk__in=identifiers, deleted_at__isnull=True, region__deleted_at__isnull=True)
            .order_by("pk")
        )
        if {plant.pk for plant in plants} != identifiers:
            raise ValueError("Every requested plant must exist and be active.")
        outcomes = []
        for plant in plants:
            today = reconstructed_at.astimezone(ZoneInfo(plant.region.timezone)).date()
            if end_date > today:
                raise ValueError("Reconstruction requires past or current local days.")
            configuration = (
                PlantConfiguration.objects.select_related("binding__provider_account")
                .filter(plant=plant, plant__current_configuration_id=F("pk"))
                .first()
            )
            calibration = (
                calibration_at(configuration, reconstructed_at)
                if configuration is not None
                else None
            )
            if calibration is None or not production_identity_current(calibration):
                raise ValueError("Every plant requires a currently usable activated calibration.")
            assert configuration is not None  # noqa: S101 -- A calibration requires its configuration.
            for offset in range((end_date - start_date).days + 1):
                outcomes.append(
                    _reconstruct_day(
                        plant,
                        configuration,
                        calibration,
                        start_date + timedelta(days=offset),
                        reconstructed_at,
                        apply=apply,
                    )
                )
        return outcomes


def reconstructed_forecast(
    plant: Plant, day: date, resolution_minutes: int, now: datetime
) -> dict[str, object] | None:
    saved = (
        ForecastReconstruction.objects.select_related("configuration__binding")
        .filter(plant=plant, target_date=day, reconstructed_at__lte=now)
        .order_by("-reconstructed_at", "-pk")
        .first()
    )
    if saved is None:
        return None
    payload = deepcopy(saved.payloads[str(resolution_minutes)])
    current = saved.configuration_id == plant.current_configuration_id
    if not current:
        cast(list[str], payload["quality_flags"]).append("previous_configuration")
    return {
        "plant_id": plant.pk,
        "date": day.isoformat(),
        "timezone": saved.configuration.binding.timezone,
        "as_of": saved.reconstructed_at.astimezone(UTC).isoformat(),
        "configuration_id": saved.configuration_id,
        "snapshot_id": saved.snapshot_id,
        "issuance_id": None,
        "calibration_id": saved.calibration_id,
        "source_kind": "forecast",
        "freshness": "fresh" if current else "stale",
        "unavailable_reason": None,
        "result": payload,
        "regional_irradiance": None,
        "outage_scenario": None,
    }
