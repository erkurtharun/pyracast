from datetime import timedelta
from typing import Any, cast

import pytest

from pyracast.plants.history import plant_outlook
from pyracast.plants.models import CalibrationRun, Plant, PlantForecast
from pyracast.plants.operations import activate_calibration, revise_plant
from tests.test_domain import NOW, System
from tests.test_plant_operations import make_plant, ready_run, saved_actuals

pytestmark = pytest.mark.django_db(transaction=True)

DAY = NOW.date() - timedelta(days=2)


def _past(system: System, plant: Plant, **options: Any) -> dict[str, Any]:
    return plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="estimated_actuals",
        **options,
    )


def _calibrated(monkeypatch: pytest.MonkeyPatch) -> tuple[System, Plant, CalibrationRun]:
    system, plant = make_plant(monkeypatch)
    saved_actuals(system, DAY)
    run = ready_run(system, plant)
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)
    return system, plant, run


def test_past_day_applies_the_active_calibration_as_a_labelled_assumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, run = _calibrated(monkeypatch)
    result = _past(system, plant)
    payload = cast(dict[str, Any], result["result"])
    assert result["source_kind"] == "estimated_actuals"
    assert result["calibration_id"] == run.pk
    assert payload["factor"] == 0.5
    assert payload["energy_mwh"] == pytest.approx(0.5 * 24 * 52)
    assert "current_calibration_applied_retrospectively" in payload["quality_flags"]
    assert "uncalibrated" not in payload["quality_flags"]
    assert result["derating_scenario"] is None
    assert not PlantForecast.objects.exists()


def test_explicit_cutoff_keeps_the_uncalibrated_historical_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = _calibrated(monkeypatch)
    result = _past(system, plant, as_of=NOW)
    payload = cast(dict[str, Any], result["result"])
    assert result["calibration_id"] is None
    assert payload["factor"] == 1
    assert "current_calibration_applied_retrospectively" not in payload["quality_flags"]


def test_calibration_from_a_changed_production_source_is_not_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, run = _calibrated(monkeypatch)
    CalibrationRun.objects.filter(pk=run.pk).update(production_snapshot_ids=[999])
    result = _past(system, plant)
    assert result["calibration_id"] is None
    assert cast(dict[str, Any], result["result"])["factor"] == 1


def test_past_day_uses_the_current_declaration_when_its_binding_archived_the_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    saved_actuals(system, DAY)
    original = plant.current_configuration_id
    assert _past(system, plant)["configuration_id"] == original
    revised = revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name="Plant",
        dc_capacity_mw=65,
        export_limit_mw=100,
    )
    result = _past(system, plant)
    payload = cast(dict[str, Any], result["result"])
    assert result["configuration_id"] == revised.current_configuration_id != original
    assert payload["energy_mwh"] == pytest.approx(24 * 26)
    assert "historical_configuration_assumed" in payload["quality_flags"]
    assert result["calibration_id"] is None


def test_plant_without_a_current_declaration_keeps_the_dated_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    saved_actuals(system, DAY)
    original = plant.current_configuration_id
    Plant.objects.filter(pk=plant.pk).update(current_configuration=None)
    result = _past(system, plant)
    assert result["configuration_id"] == original
    assert result["calibration_id"] is None
    assert cast(dict[str, Any], result["result"])["factor"] == 1
