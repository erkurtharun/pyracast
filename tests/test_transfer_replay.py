from datetime import date, timedelta
from typing import Any, cast

import pytest

from pyracast.connections.models import SiteBinding
from pyracast.plants.forecasting import plant_forecast
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import Plant, PlantConfiguration, PlantForecast
from pyracast.plants.numerics import MODEL_VERSION
from pyracast.plants.operations import activate_calibration
from pyracast.plants.tracking import MODEL_VERSION as TRACKING_MODEL_VERSION
from tests.test_domain import NOW, System
from tests.test_incident_calibration import incident
from tests.test_plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)

DAY = date(2026, 11, 10)


def _declare_tracking(system: System, plant: Plant) -> None:
    SiteBinding.objects.filter(pk=system.binding.pk).update(dc_capacity_kw=1)
    PlantConfiguration.objects.filter(plant=plant).update(mounting_type="tracking")


def _replay(system: System, plant: Plant, resolution_minutes: int) -> dict[str, Any]:
    return plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=resolution_minutes,
    )


def test_saved_forecast_keeps_its_calibration_after_the_transfer_model_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = ready_run(system, plant)
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)
    publish_snapshot(snapshot_for(system, DAY, collected_at=NOW - timedelta(hours=1)))
    saved = _replay(system, plant, 60)
    assert saved["calibration_id"] == run.pk
    assert cast(dict[str, Any], saved["result"])["model_version"] == MODEL_VERSION
    _declare_tracking(system, plant)
    assert _replay(system, plant, 60) == saved
    assert PlantForecast.objects.count() == 1
    fresh = _replay(system, plant, 30)
    payload = cast(dict[str, Any], fresh["result"])
    assert fresh["calibration_id"] is None
    assert payload["factor"] == 1
    assert payload["model_version"] == TRACKING_MODEL_VERSION
    assert "uncalibrated" in payload["quality_flags"]
    assert _replay(system, plant, 30) == fresh
    assert PlantForecast.objects.count() == 2


def test_outage_scenario_replays_the_transfer_model_of_its_saved_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, DAY, collected_at=NOW - timedelta(hours=1)))
    _replay(system, plant, 60)
    _declare_tracking(system, plant)
    incident(system, plant, NOW - timedelta(hours=1), None)
    result = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        as_of=NOW,
    )
    normal = cast(dict[str, Any], result["result"])
    scenario = cast(dict[str, Any], result["outage_scenario"])["result"]
    assert normal["model_version"] == scenario["model_version"] == MODEL_VERSION
    assert scenario["partial_energy_mwh"] == pytest.approx(0.5 * normal["partial_energy_mwh"])
    assert result["derating_scenario"] is None
