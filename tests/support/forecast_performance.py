from datetime import date, datetime, timedelta

from django.db import transaction

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_archive import prepare_forecast_issuances, save_forecast_issuances
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    ProductionObservation,
)
from tests.support.domain import System

DAY = date(2026, 11, 8)


def publish(source: ForecastSnapshot, *, published_at: datetime | None = None) -> None:
    published_at = source.collected_at if published_at is None else published_at
    source.available_at = published_at
    source.save()
    source.job.status = "succeeded"
    source.job.save()
    with transaction.atomic():
        plant = Plant.objects.select_for_update().get(region_id=source.binding.region_id)
        save_forecast_issuances(
            prepare_forecast_issuances(source), source, published_at, plant=plant
        )


def measurements(system: System, configuration: PlantConfiguration, energy: float) -> None:
    start, end = local_day_bounds(DAY, system.binding.timezone)
    while start < end:
        ProductionObservation.objects.create(
            plant_id=configuration.plant_id,
            configuration=configuration,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            energy_mwh=energy,
            recorded_by=system.member,
            source="meter",
        )
        start += timedelta(hours=1)


def selected_factor(system: System, configuration: PlantConfiguration, at: datetime) -> None:
    run = CalibrationRun.objects.create(
        plant_id=configuration.plant_id,
        configuration=configuration,
        binding=system.binding,
        requested_by=system.member,
        window_start=DAY - timedelta(days=10),
        window_end=DAY - timedelta(days=4),
        cutoff_at=at,
        completed_at=at,
        status="ready",
        factor=0.8,
    )
    CalibrationRun.objects.filter(pk=run.pk).update(created_at=at)
    activation = CalibrationActivation.objects.create(
        plant_id=configuration.plant_id,
        configuration=configuration,
        calibration=run,
        actor=system.member,
    )
    CalibrationActivation.objects.filter(pk=activation.pk).update(created_at=at)
