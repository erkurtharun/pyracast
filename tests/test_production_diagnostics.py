from copy import deepcopy
from datetime import date, timedelta
from typing import Any, cast

import pytest
from django.core.exceptions import ValidationError

from pyracast.connections.models import SiteBinding
from pyracast.plants.automation import set_calibration_automation
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
)
from pyracast.plants.operations import activate_calibration
from pyracast.plants.tracking import MODEL_VERSION as TRACKING_MODEL_VERSION
from tests.support.calibration_automation import declare_native_sources, evaluated_run, promote
from tests.support.domain import NOW, System
from tests.support.incident_calibration import incident
from tests.support.plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)

SUSPECTED = {
    "policy": "strong-unclipped-power-diagnostics-v2",
    "status": "suspected",
    "baseline_factor": 0.5,
    "performance_ratio": 0.8,
    "threshold": 0.92,
    "window_start": "2026-10-30",
    "window_end": "2026-11-08",
    "days": [
        {
            "day": "2026-11-08",
            "performance_index": 0.8,
            "hours": 6.0,
            "status": "shortfall",
            "observed_zero": False,
        }
    ],
    "limit_reached": False,
    "reference_kind": "active_calibration",
    "latest_matched_day": "2026-11-08",
    "latest_day_status": "shortfall",
    "latest_performance_index": 0.8,
}


def _live_plant(monkeypatch: pytest.MonkeyPatch) -> tuple[System, Plant, CalibrationRun]:
    system, plant = make_plant(monkeypatch)
    PlantConfiguration.objects.filter(plant=plant).update(created_at=NOW)
    run = ready_run(system, plant)
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)
    publish_snapshot(snapshot_for(system, NOW.date(), collected_at=NOW))
    return system, plant, run


def _outlook(system: System, plant: Plant, **options: Any) -> dict[str, Any]:
    return plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=NOW.date(),
        resolution_minutes=60,
        **options,
    )


def _suspect(run: CalibrationRun, **changes: object) -> None:
    run.result["derating"] = SUSPECTED | changes
    run.save(update_fields=("result",))


@pytest.mark.parametrize("status", ["suspected", "normal", "insufficient"])
def test_live_production_diagnostic_does_not_change_the_forecast(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    system, plant, run = _live_plant(monkeypatch)
    baseline = _outlook(system, plant)
    assert baseline["production_diagnostic"] is None
    normal = deepcopy(baseline["result"])
    _suspect(run, status=status)
    result = _outlook(system, plant)
    diagnostic = cast(dict[str, Any], result["production_diagnostic"])
    assert result["outage_scenario"] is None
    assert diagnostic == SUSPECTED | {
        "status": status,
        "evaluated_at": NOW.isoformat(),
        "calibration_id": run.pk,
        "evidence_state": "current",
    }
    assert result["result"] == normal
    assert "result" not in diagnostic
    assert "derating_scenario" not in result


@pytest.mark.parametrize("inverter", [25, None])
def test_production_diagnostic_does_not_infer_lost_capacity(
    monkeypatch: pytest.MonkeyPatch, inverter: int | None
) -> None:
    system, plant, run = _live_plant(monkeypatch)
    PlantConfiguration.objects.filter(plant=plant).update(inverter_capacity_mw=inverter)
    baseline = deepcopy(_outlook(system, plant)["result"])
    _suspect(run)
    result = _outlook(system, plant)
    assert result["production_diagnostic"] is not None
    assert result["result"] == baseline
    configuration = PlantConfiguration.objects.get(plant=plant)
    assert configuration.dc_capacity_mw == 130
    assert configuration.inverter_capacity_mw == inverter
    assert configuration.export_limit_mw == 100


@pytest.mark.parametrize(
    "changes",
    [
        {"baseline_factor": 0.75},
        {"policy": "strong-unclipped-power-median-v1"},
        {"latest_matched_day": None},
        {"latest_matched_day": "invalid"},
        {"latest_matched_day": "2026-11-09"},
        {"latest_matched_day": "2026-11-10"},
        {"other": {"result": {"transfer_model": TRACKING_MODEL_VERSION}}},
        {"other": {"production_snapshot_ids": [999]}},
        {"explicit_cutoff": True},
    ],
)
def test_diagnostic_rejects_incompatible_latest_evidence_without_falling_back(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    system, plant, run = _live_plant(monkeypatch)
    fields = ("policy", "baseline_factor", "latest_matched_day")
    _suspect(run, **{key: changes[key] for key in fields if key in changes})
    options: dict[str, Any] = {}
    if "other" in changes:
        other = CalibrationRun.objects.get(pk=run.pk)
        other.pk = None
        other.result = other.result | changes["other"].get("result", {})
        other.production_snapshot_ids = changes["other"].get("production_snapshot_ids", [])
        other.save()
    if changes.get("explicit_cutoff"):
        options["as_of"] = NOW - timedelta(days=1)
        publish_snapshot(
            snapshot_for(system, NOW.date(), collected_at=NOW - timedelta(days=1, hours=2))
        )
    assert _outlook(system, plant, **options)["production_diagnostic"] is None


@pytest.mark.parametrize("status", ["ready", "rejected", "insufficient"])
def test_uncalibrated_outlook_reports_persisted_evidence_without_applying_a_factor(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    system, plant, run = _live_plant(monkeypatch)
    CalibrationActivation.objects.all().delete()
    _suspect(run, baseline_factor=1.0, reference_kind="uncalibrated_reference")
    run.status = status
    run.save(update_fields=("status",))
    result = _outlook(system, plant)
    assert result["calibration_id"] is None
    diagnostic = cast(dict[str, Any], result["production_diagnostic"])
    assert diagnostic["reference_kind"] == "uncalibrated_reference"
    assert diagnostic["calibration_id"] == run.pk
    assert cast(dict[str, Any], result["result"])["factor"] == 1


@pytest.mark.parametrize(
    ("latest_day", "evidence_state"), [("2026-11-07", "current"), ("2026-11-06", "stale")]
)
def test_evidence_freshness_is_a_display_state_and_does_not_change_output(
    monkeypatch: pytest.MonkeyPatch, latest_day: str, evidence_state: str
) -> None:
    system, plant, run = _live_plant(monkeypatch)
    baseline = deepcopy(_outlook(system, plant)["result"])
    _suspect(run, latest_matched_day=latest_day)
    result = _outlook(system, plant)
    diagnostic = cast(dict[str, Any], result["production_diagnostic"])
    assert diagnostic["evidence_state"] == evidence_state
    assert result["result"] == baseline


def test_declared_outage_scenario_remains_separate_from_compatible_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, run = _live_plant(monkeypatch)
    _suspect(run)
    baseline = deepcopy(_outlook(system, plant)["result"])
    incident(system, plant, NOW, None)
    result = _outlook(system, plant)
    assert result["production_diagnostic"] is not None
    assert result["result"] == baseline
    scenario = cast(dict[str, Any], result["outage_scenario"])
    assert scenario["result"]["intervals"][-1]["mean_power_mw"] == pytest.approx(13)


def test_revised_training_incidents_hide_uncalibrated_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, run = _live_plant(monkeypatch)
    CalibrationActivation.objects.all().delete()
    _suspect(run, baseline_factor=1.0, reference_kind="uncalibrated_reference")
    assert _outlook(system, plant)["production_diagnostic"] is not None
    incident(system, plant, NOW - timedelta(days=3), NOW - timedelta(days=2))
    result = _outlook(system, plant)
    assert result["production_diagnostic"] is None
    assert cast(dict[str, Any], result["result"])["factor"] == 1


def test_first_calibration_is_promoted_despite_a_suspected_derating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = evaluated_run(system, plant)
    _suspect(run)
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    assert CalibrationActivation.objects.get().calibration_id == run.pk


def test_suspected_derating_pauses_promotion_over_an_active_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    first = evaluated_run(system, plant)
    first.window_start, first.window_end = date(2026, 10, 1), date(2026, 10, 30)
    first.result["holdout_day_dates"] = [date(2026, 10, day).isoformat() for day in range(24, 31)]
    declare_native_sources(first)
    first.save()
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, first.pk)
    candidate = evaluated_run(system, plant)
    candidate.window_start, candidate.window_end = date(2026, 10, 1), date(2026, 11, 6)
    candidate.result["holdout_day_dates"] = [
        (date(2026, 10, 31) + timedelta(days=offset)).isoformat() for offset in range(7)
    ]
    candidate.result["derating"] = dict(SUSPECTED)
    declare_native_sources(candidate)
    candidate.save()
    assert not promote(plant.pk, candidate)
    assert PlantEvent.objects.latest("pk").details["reason"] == "derating_suspected"
    assert CalibrationActivation.objects.latest("pk").calibration_id == first.pk
    candidate.result["derating"] = SUSPECTED | {"status": "normal"}
    candidate.save()
    assert promote(plant.pk, candidate)
    assert CalibrationActivation.objects.latest("pk").calibration_id == candidate.pk


def test_tracking_outlook_models_geometry_and_ignores_factors_from_another_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, run = _live_plant(monkeypatch)
    direct = cast(dict[str, Any], _outlook(system, plant)["result"])
    assert direct["model_version"] == "reference-power-transfer-v1"
    assert "geometry_not_modeled" in direct["quality_flags"]
    SiteBinding.objects.filter(pk=system.binding.pk).update(dc_capacity_kw=1)
    PlantConfiguration.objects.filter(plant=plant).update(mounting_type="tracking")
    result = _outlook(system, plant)
    payload = cast(dict[str, Any], result["result"])
    assert result["calibration_id"] is None
    assert payload["factor"] == 1
    assert payload["model_version"] == TRACKING_MODEL_VERSION
    assert "assumed_tracker_geometry" in payload["quality_flags"]
    assert "geometry_not_modeled" not in payload["quality_flags"]
    powers = [row["mean_power_mw"] for row in payload["intervals"]]
    assert max(powers) > 1.3 * 52
    assert min(powers) == pytest.approx(52, rel=0.2)
    legacy = ready_run(system, plant)
    with pytest.raises(ValidationError):
        activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, legacy.pk)
    CalibrationRun.objects.filter(pk=run.pk).update(
        result=run.result | {"transfer_model": TRACKING_MODEL_VERSION}
    )
    calibrated = _outlook(system, plant)
    assert calibrated["calibration_id"] == run.pk
    assert cast(dict[str, Any], calibrated["result"])["factor"] == 0.5
