from datetime import date, timedelta

from django.db import transaction

from pyracast.accounts.models import Tenant
from pyracast.irradiance import local_day_bounds
from pyracast.plants.automation import lock_enabler, promote_automatic_calibration
from pyracast.plants.calibration_data import current_source_validation
from pyracast.plants.models import (
    CalibrationRun,
    Plant,
)
from pyracast.plants.production_changes import ObservationInput, import_observations
from pyracast.regions.models import Region
from tests.support.domain import NOW, System
from tests.support.plant_operations import ready_run
from tests.support.plant_training import actual_snapshot_for


def declare_native_sources(run: CalibrationRun) -> None:
    day = run.window_start
    included = []
    while day <= run.window_end:
        included.append(day.isoformat())
        day += timedelta(days=1)
    run.result["included_day_dates"] = included
    run.result["valid_days"] = len(included)
    run.result["training_day_dates"] = [
        day for day in included if day not in run.result["holdout_day_dates"]
    ]
    run.result["training_days"] = len(run.result["training_day_dates"])
    run.dataset_manifest["binding_id"] = run.binding_id
    run.dataset_manifest["sources"] = {
        day: {
            "snapshot_id": index + 1,
            "binding_id": run.binding_id,
            "source_kind": "estimated_actuals",
            "source_product": "rooftop_pv_power",
            "reference_import": None,
        }
        for index, day in enumerate(included)
    }
    run.result["current_source_validation"] = current_source_validation(
        run.dataset_manifest, run.result, run.binding_id
    )


def evaluated_run(system: System, plant: Plant) -> CalibrationRun:
    run = ready_run(system, plant)
    run.window_start, run.window_end = date(2026, 10, 9), date(2026, 11, 7)
    run.assessment = "evaluated"
    run.result.update(
        {
            "valid_days": 30,
            "holdout_days": 7,
            "holdout_day_dates": [date(2026, 11, day).isoformat() for day in range(1, 8)],
            "candidate": {"interval_mae_mw": 0.5, "daily_mae_mwh": 0.5},
            "incumbent": {"interval_mae_mw": 1.0, "daily_mae_mwh": 1.0},
        }
    )
    declare_native_sources(run)
    run.save()
    return run


def promote(plant_id: int, run: CalibrationRun) -> bool:
    with transaction.atomic():
        identity = Plant.objects.select_related("region").get(pk=plant_id)
        enabler = lock_enabler(plant_id)
        Tenant.objects.select_for_update().get(pk=identity.region.tenant_id)
        Region.objects.select_for_update().get(pk=identity.region_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id)
        return promote_automatic_calibration(plant, run, NOW, enabler)


def import_fit_history(system: System, plant: Plant) -> None:
    """Thirty days of hourly production with their actuals; the import requests the run."""
    rows: list[ObservationInput] = []
    for offset in range(30):
        day = date(2026, 10, 9) + timedelta(days=offset)
        actual_snapshot_for(system, day)
        start, end = local_day_bounds(day, system.region.timezone)
        while start < end:
            rows.append(ObservationInput(start, start + timedelta(hours=1), 26))
            start += timedelta(hours=1)
    assert plant.current_configuration_id is not None
    import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="MWh",
        observations=rows,
    )
