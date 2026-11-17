from datetime import date, timedelta

from django.db import transaction

from pyracast.accounts.models import Tenant
from pyracast.plants.automation import (
    promote_automatic_calibration,
)
from pyracast.plants.calibration_data import current_source_validation
from pyracast.plants.models import (
    CalibrationRun,
    Plant,
)
from pyracast.regions.models import Region
from tests.support.domain import NOW, System
from tests.support.plant_operations import ready_run


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
        Tenant.objects.select_for_update().get(pk=identity.region.tenant_id)
        Region.objects.select_for_update().get(pk=identity.region_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id)
        return promote_automatic_calibration(plant, run, NOW)
