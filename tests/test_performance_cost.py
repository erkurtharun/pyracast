import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from pyracast.plants.models import CalibrationActivation, CalibrationRun
from pyracast.plants.performance import forecast_performance
from tests.support.domain import NOW
from tests.support.performance_history import FIRST, LAST, performance_history
from tests.support.performance_pins import performance_numbers
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)

PINS = json.loads((Path(__file__).parent / "support" / "performance_pins.json").read_text())
COMPARISONS = ("archived_forecast", "historical_model")


def _read(plant: Any, comparison: str, *, daylight_only: bool = True, start: date = FIRST) -> Any:
    return forecast_performance(
        plant, start_date=start, end_date=LAST, comparison=comparison, daylight_only=daylight_only
    )


@pytest.mark.parametrize("comparison", COMPARISONS)
@pytest.mark.parametrize("daylight_only", [False, True])
def test_performance_numbers_equal_the_pinned_numbers(
    monkeypatch: pytest.MonkeyPatch, comparison: str, daylight_only: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshots, calibrations = performance_history(system, plant)
    result = _read(plant, comparison, daylight_only=daylight_only)
    numbers = json.loads(
        json.dumps(performance_numbers(result, snapshots, calibrations), default=str)
    )
    assert numbers == PINS[f"{comparison}-{daylight_only}"]


@pytest.mark.parametrize("comparison", COMPARISONS)
def test_ninety_days_of_performance_take_at_most_eight_queries(
    monkeypatch: pytest.MonkeyPatch, comparison: str
) -> None:
    system, plant = make_plant(monkeypatch)
    performance_history(system, plant)
    with CaptureQueriesContext(connection) as queries:
        result = _read(plant, comparison, start=LAST - timedelta(days=89))
    assert result["requested_days"] == 90
    assert result["matched_days"] == 4 + (comparison == "archived_forecast")
    assert len(queries) <= 8


@pytest.mark.parametrize("comparison", COMPARISONS)
def test_performance_query_count_does_not_grow_with_activation_history(
    monkeypatch: pytest.MonkeyPatch, comparison: str
) -> None:
    system, plant = make_plant(monkeypatch)
    performance_history(system, plant)
    with CaptureQueriesContext(connection) as before:
        expected = _read(plant, comparison)
    stale = CalibrationRun.objects.get(incident_revision_ids=[999_999])
    for offset in range(20):
        run = CalibrationRun.objects.get(pk=stale.pk)
        run.pk = None
        run.save()
        activation = CalibrationActivation.objects.create(
            plant=plant, configuration=run.configuration, calibration=run, actor=system.member
        )
        CalibrationActivation.objects.filter(pk=activation.pk).update(
            created_at=NOW - timedelta(days=1, minutes=offset)
        )
    with CaptureQueriesContext(connection) as after:
        result = _read(plant, comparison)
    assert len(after) == len(before)
    assert result["metrics"] == expected["metrics"]


def test_historical_days_state_whether_their_calibration_trained_on_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    performance_history(system, plant)
    days = {str(day["date"]): day for day in _read(plant, "historical_model")["days"]}
    assert {day: row["calibration_in_sample"] for day, row in days.items()} == {
        "2026-11-03": False,
        "2026-11-04": False,
        "2026-11-05": True,
        "2026-11-06": None,
        "2026-11-07": True,
        "2026-11-08": True,
    }
    archived = _read(plant, "archived_forecast")["days"]
    assert {day["calibration_in_sample"] for day in archived} == {False, None}


@pytest.mark.parametrize("comparison", COMPARISONS)
def test_accuracy_leads_with_relative_error_and_out_of_sample_skill(
    monkeypatch: pytest.MonkeyPatch, comparison: str
) -> None:
    system, plant = make_plant(monkeypatch)
    performance_history(system, plant)
    result = _read(plant, comparison)
    matched = [day for day in result["days"] if day["status"] == "matched"]
    assert result["relative_error"] == sum(abs(day["error_mwh"]) for day in matched) / sum(
        day["measured_energy_mwh"] for day in matched
    )
    scored = [
        day
        for day in matched
        if comparison == "archived_forecast" or day["calibration_in_sample"] is False
    ]
    assert len(scored) == (5 if comparison == "archived_forecast" else 2)
    assert result["skill_score"] == 1 - sum(abs(day["error_mwh"]) for day in scored) / sum(
        abs(day["baseline_energy_mwh"] - day["measured_energy_mwh"]) for day in scored
    )
    assert result["skill_days"] == len(scored)


def test_accuracy_is_absent_without_matched_days(monkeypatch: pytest.MonkeyPatch) -> None:
    _, plant = make_plant(monkeypatch)
    result = _read(plant, "historical_model")
    assert result["matched_days"] == 0
    assert (result["relative_error"], result["skill_score"], result["skill_days"]) == (
        None,
        None,
        0,
    )
