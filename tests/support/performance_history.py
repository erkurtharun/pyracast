from datetime import date, datetime, timedelta

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    ProductionObservation,
)
from tests.support.domain import NOW, System
from tests.support.forecast_performance import publish
from tests.support.plant_operations import half_hours, snapshot_for

FIRST = date(2026, 11, 3)
LAST = date(2026, 11, 8)


def estimated_actuals(
    system: System,
    day: date,
    power_kw: float,
    collected_at: datetime,
    *,
    hours: tuple[int, int] = (0, 24),
) -> ForecastSnapshot:
    start, _ = local_day_bounds(day, system.binding.timezone)
    snapshot = snapshot_for(
        system,
        day,
        collected_at=collected_at,
        source_kind="estimated_actuals",
        available_at=collected_at,
        normalized_intervals=half_hours(
            start + timedelta(hours=hours[0]), start + timedelta(hours=hours[1]), power_kw
        ),
    )
    snapshot.job.endpoint, snapshot.job.status = "estimated_actuals", "succeeded"
    snapshot.job.save(update_fields=("endpoint", "status"))
    return snapshot


def activated_calibration(
    system: System,
    configuration: PlantConfiguration,
    factor: float,
    first_training_day: date,
    activated_at: datetime,
    *,
    incident_revision_ids: list[int] | None = None,
) -> CalibrationRun:
    run = CalibrationRun.objects.create(
        plant_id=configuration.plant_id,
        configuration=configuration,
        binding=system.binding,
        requested_by=system.member,
        window_start=first_training_day,
        window_end=first_training_day + timedelta(days=6),
        cutoff_at=activated_at,
        completed_at=activated_at,
        status="ready",
        factor=factor,
        result={
            "training_day_dates": [
                (first_training_day + timedelta(days=offset)).isoformat() for offset in range(7)
            ]
        },
        incident_revision_ids=incident_revision_ids or [],
    )
    CalibrationRun.objects.filter(pk=run.pk).update(created_at=activated_at)
    activation = CalibrationActivation.objects.create(
        plant_id=configuration.plant_id,
        configuration=configuration,
        calibration=run,
        actor=system.member,
    )
    CalibrationActivation.objects.filter(pk=activation.pk).update(created_at=activated_at)
    return run


def _observations(
    system: System, plant: Plant, configuration: PlantConfiguration, day: date, skip: set[int]
) -> None:
    start, end = local_day_bounds(day, system.binding.timezone)
    hour = 0
    while start < end:
        if hour not in skip:
            ProductionObservation.objects.create(
                plant=plant,
                configuration=configuration,
                starts_at=start,
                ends_at=start + timedelta(hours=1),
                energy_mwh=20 + hour + day.day / 10,
                source="meter",
                recorded_by=system.member,
            )
        start += timedelta(hours=1)
        hour += 1


def performance_history(system: System, plant: Plant) -> tuple[dict[int, str], dict[int, str]]:
    """Six past days mixing legacy, composed and competing reference sources, archived
    forecasts, three activations (one invalidated by its outage history) and gaps; returns
    snapshot and calibration labels by id."""
    configuration = plant.current_configuration
    assert configuration is not None
    labels: dict[int, str] = {}
    early = activated_calibration(
        system, configuration, 0.9, date(2026, 10, 20), NOW - timedelta(days=12)
    )
    late = activated_calibration(
        system, configuration, 0.8, date(2026, 11, 5), NOW - timedelta(days=4)
    )
    stale = activated_calibration(
        system,
        configuration,
        0.7,
        date(2026, 10, 25),
        NOW - timedelta(days=2),
        incident_revision_ids=[999_999],
    )
    calibrations = {early.pk: "early", late.pk: "late", stale.pk: "stale"}
    for offset, power in enumerate((0.35, 0.42, 0.5, 0.31, 0.45, 0.38)):
        day = FIRST + timedelta(days=offset)
        start, end = local_day_bounds(day, system.binding.timezone)
        forecast = snapshot_for(
            system, day, power_kw=power, collected_at=start - timedelta(hours=8)
        )
        publish(forecast)
        labels[forecast.pk] = f"forecast-{day.day}"
        later = snapshot_for(
            system, day, power_kw=power + 0.1, collected_at=start - timedelta(hours=3)
        )
        publish(later)
        labels[later.pk] = f"later-forecast-{day.day}"
        skip = {12} if day.day == 7 else set()
        _observations(system, plant, configuration, day, skip)
    sources = {
        3: [("legacy", 0.3, 1, (0, 24))],
        4: [
            ("legacy-old", 0.25, 1, (0, 24)),
            ("morning", 0.3, 0, (0, 12)),
            ("evening", 0.5, 1, (12, 24)),
        ],
        5: [("first", 0.33, 1, (0, 24)), ("second", 0.36, 2, (0, 24))],
        7: [("partial", 0.4, 1, (0, 18))],
        8: [("legacy", 0.37, 1, (0, 24)), ("scaled", 0.39, 3, (0, 24))],
    }
    for day_number, rows in sources.items():
        day = date(2026, 11, day_number)
        start, end = local_day_bounds(day, system.binding.timezone)
        for label, power, delay, hours in rows:
            collected = (
                start + timedelta(hours=hours[1], minutes=5)
                if delay == 0
                else end + timedelta(hours=delay)
            )
            snapshot = estimated_actuals(system, day, power, collected, hours=hours)
            labels[snapshot.pk] = f"{label}-{day_number}"
    return labels, calibrations
