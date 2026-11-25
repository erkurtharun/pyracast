import logging
from datetime import date, timedelta
from typing import Any, cast

import pytest
from django.test import Client

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.plants.activation import activate_calibration, reset_calibration
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import CalibrationRun, Plant
from pyracast.plants.operations import revise_plant
from pyracast.plants.parameters import PlantParameters
from tests.support.domain import NOW, System
from tests.support.invariants import stored_again
from tests.support.plant_operations import (
    make_plant,
    publish_snapshot,
    ready_run,
    saved_actuals,
    snapshot_for,
)

pytestmark = pytest.mark.django_db(transaction=True)

DAY = NOW.date() - timedelta(days=2)
FAULTS = "pyracast.forecasts.faults"


def _past(system: System, plant: Plant, day: date = DAY, **options: Any) -> dict[str, Any]:
    return plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=day,
        resolution_minutes=60,
        source="estimated_actuals",
        **options,
    )


def _activate(
    system: System, plant: Plant, training_start: object, factor: float = 0.5
) -> CalibrationRun:
    run = ready_run(system, plant)
    run.factor = factor
    run.result["training_day_dates"] = training_start
    run.save()
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)
    return run


def _calibrated(
    monkeypatch: pytest.MonkeyPatch, training_start: object
) -> tuple[System, Plant, CalibrationRun]:
    system, plant = make_plant(monkeypatch)
    saved_actuals(system, DAY)
    return system, plant, _activate(system, plant, training_start)


def test_past_day_inside_the_training_period_uses_that_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, run = _calibrated(monkeypatch, [DAY.isoformat()])
    result = _past(system, plant)
    payload = cast(dict[str, Any], result["result"])
    assert result["source_kind"] == "estimated_actuals"
    assert result["calibration_id"] == run.pk
    assert payload["factor"] == 0.5
    assert payload["energy_mwh"] == pytest.approx(0.5 * 24 * 52)
    assert "retrospective_calibration" in payload["quality_flags"]
    assert "uncalibrated" not in payload["quality_flags"]
    assert result["production_diagnostic"] is None


@pytest.mark.parametrize(
    "training_start",
    [[(DAY + timedelta(days=1)).isoformat()], None, [], [20261107], ["not-a-date"]],
)
def test_calibration_is_not_applied_before_a_known_training_period(
    monkeypatch: pytest.MonkeyPatch, training_start: object
) -> None:
    system, plant, _ = _calibrated(monkeypatch, training_start)
    result = _past(system, plant)
    payload = cast(dict[str, Any], result["result"])
    assert result["calibration_id"] is None
    assert payload["factor"] == 1
    assert "uncalibrated" in payload["quality_flags"]
    assert "retrospective_calibration" not in payload["quality_flags"]


def test_each_day_uses_the_newest_calibration_whose_period_had_begun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, first = _calibrated(monkeypatch, [(DAY - timedelta(days=9)).isoformat()])
    later_day = DAY + timedelta(days=1)
    saved_actuals(system, later_day)
    second = _activate(system, plant, [later_day.isoformat()], factor=0.6)
    earlier = _past(system, plant)
    later = _past(system, plant, later_day)
    assert earlier["calibration_id"] == first.pk
    assert cast(dict[str, Any], earlier["result"])["factor"] == 0.5
    assert later["calibration_id"] == second.pk
    assert cast(dict[str, Any], later["result"])["factor"] == 0.6
    reset_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk)
    assert _past(system, plant)["calibration_id"] is None
    assert _past(system, plant, later_day)["calibration_id"] is None


def test_explicit_cutoff_keeps_the_uncalibrated_historical_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = _calibrated(monkeypatch, [DAY.isoformat()])
    result = _past(system, plant, as_of=NOW)
    payload = cast(dict[str, Any], result["result"])
    assert result["calibration_id"] is None
    assert payload["factor"] == 1
    assert "retrospective_calibration" not in payload["quality_flags"]


def test_calibration_from_a_changed_production_source_is_not_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, run = _calibrated(monkeypatch, [DAY.isoformat()])
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
        PlantParameters(name="Plant", dc_capacity_mw=65, export_limit_mw=100),
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


def _read_with_fallback(system: System, plant: Plant) -> tuple[int, dict[str, Any]]:
    client = Client()
    client.force_login(system.member)
    response = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/",
        {"date": DAY.isoformat(), "data_source": "auto", "resolution_minutes": "60"},
    )
    return response.status_code, cast(dict[str, Any], response.json())


def _actuals_with_power(system: System, power: object) -> ForecastSnapshot:
    publish_snapshot(snapshot_for(system, DAY))
    source = saved_actuals(system, DAY)
    rows = [dict(row) for row in source.normalized_intervals]
    rows[20]["power_kw"] = power
    return stored_again(source, normalized_intervals=rows)


@pytest.mark.parametrize(
    ("power", "fault"),
    [(True, "ValueError"), ("0.4", "ValueError"), (10**400, "OverflowError")],
    ids=["boolean", "text", "beyond-a-float"],
)
def test_a_damaged_estimated_actual_refuses_the_day_instead_of_the_saved_forecast(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, power: object, fault: str
) -> None:
    system, plant = make_plant(monkeypatch)
    source = _actuals_with_power(system, power)
    with caplog.at_level(logging.WARNING, logger=FAULTS):
        status, answer = _read_with_fallback(system, plant)
    assert (status, answer["code"]) == (400, "calculation_out_of_range")
    [warning] = [record.getMessage() for record in caplog.records if record.name == FAULTS]
    assert f"record=forecast_snapshot:{source.pk} error={fault}" in warning


def test_an_explicitly_missing_estimated_actual_leaves_the_saved_forecast_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    _actuals_with_power(system, None)
    status, answer = _read_with_fallback(system, plant)
    assert status == 200, answer
    assert answer["source_kind"] == "forecast"
    assert "forecast_fallback" in answer["result"]["quality_flags"]
