from datetime import date

import pytest

from pyracast.plants.activation import activate_calibration
from pyracast.plants.models import CalibrationActivation, CalibrationRun
from pyracast.plants.preparation import prepare_calibration
from pyracast.plants.training import run_calibration
from tests.support.calibration_preparation import add_days
from tests.support.domain import NOW
from tests.support.plant_operations import make_plant, ready_run

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("days", [1, 6])
def test_short_history_records_diagnostics_without_activating_a_factor(
    monkeypatch: pytest.MonkeyPatch, days: int
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, days, daylight_only=True)
    run = CalibrationRun.objects.get()
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "insufficient"
    assert run.factor is None
    assert run.result["valid_days"] == days
    diagnostic = run.result["derating"]
    assert diagnostic["reference_kind"] == "uncalibrated_reference"
    assert diagnostic["latest_matched_day"] == date(2026, 11, days).isoformat()
    assert diagnostic["latest_day_status"] == "shortfall"
    assert not CalibrationActivation.objects.exists()
    assert prepare_calibration(plant.pk) is None


def test_identity_calibration_remains_an_explicit_comparison_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    active = ready_run(system, plant)
    active.factor = 1.0
    active.save(update_fields=("factor",))
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, active.pk)
    add_days(system, plant, 1, 1, daylight_only=True)
    run = CalibrationRun.objects.exclude(pk=active.pk).get()
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.result["derating"]["reference_kind"] == "active_calibration"
    assert run.result["derating"]["baseline_factor"] == 1.0
    assert CalibrationActivation.objects.get().calibration_id == active.pk
