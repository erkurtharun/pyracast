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
from tests.test_calibration_automation import declare_native_sources, evaluated_run, promote
from tests.test_domain import NOW, System
from tests.test_plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)

SUSPECTED = {
    "policy": "strong-unclipped-power-median-v1",
    "status": "suspected",
    "baseline_factor": 0.5,
    "performance_ratio": 0.8,
    "threshold": 0.92,
    "window_start": "2026-10-30",
    "window_end": "2026-11-08",
    "days": [{"day": "2026-11-08", "performance_index": 0.8, "hours": 6.0}],
    "limit_reached": False,
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


def test_live_outlook_adds_a_conditional_scenario_for_a_suspected_derating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, run = _live_plant(monkeypatch)
    assert _outlook(system, plant)["derating_scenario"] is None
    _suspect(run)
    result = _outlook(system, plant)
    normal = cast(dict[str, Any], result["result"])
    scenario = cast(dict[str, Any], result["derating_scenario"])
    assert result["outage_scenario"] is None
    assert scenario["status"] == "suspected"
    assert scenario["calibration_id"] == run.pk
    assert scenario["performance_ratio"] == 0.8
    assert scenario["days"] == SUSPECTED["days"]
    assert scenario["inverter_limit_scaled"] is False
    affected = scenario["result"]
    assert affected["partial_energy_mwh"] == pytest.approx(0.8 * normal["partial_energy_mwh"])
    assert affected["factor"] == normal["factor"] == 0.5
    assert affected["model_version"] == normal["model_version"]
    assert affected["provenance"]["scenario_policy"] == "suspected-derating-v1"
    assert affected["provenance"]["derating_calibration_id"] == run.pk
    assert "conditional_on_suspected_derating" in affected["quality_flags"]
    assert [row["source_snapshot_ids"] for row in affected["intervals"]] == [
        row["source_snapshot_ids"] for row in normal["intervals"]
    ]


def test_inverter_capacity_follows_the_ratio_but_the_export_limit_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, run = _live_plant(monkeypatch)
    PlantConfiguration.objects.filter(plant=plant).update(inverter_capacity_mw=25)
    _suspect(run)
    scenario = cast(dict[str, Any], _outlook(system, plant)["derating_scenario"])
    assert scenario["inverter_limit_scaled"] is True
    assert max(row["mean_power_mw"] for row in scenario["result"]["intervals"]) == 20
    _suspect(run, limit_reached=True)
    reached = cast(dict[str, Any], _outlook(system, plant)["derating_scenario"])
    assert reached["inverter_limit_scaled"] is False
    assert max(row["mean_power_mw"] for row in reached["result"]["intervals"]) == pytest.approx(
        0.8 * 26
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "normal"},
        {"baseline_factor": 0.75},
        {"window_end": "2026-10-29"},
        {"window_end": None},
        {"other": {"result": {"transfer_model": TRACKING_MODEL_VERSION}}},
        {"other": {"production_snapshot_ids": [999]}},
        {"explicit_cutoff": True},
    ],
)
def test_scenario_requires_fresh_evidence_against_the_factor_in_use(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    system, plant, run = _live_plant(monkeypatch)
    fields = ("status", "baseline_factor", "window_end")
    _suspect(run, **{key: changes[key] for key in fields if key in changes})
    options: dict[str, Any] = {}
    if "other" in changes:
        # A newer run is the latest evidence but cannot vouch for the factor in use.
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
    assert _outlook(system, plant, **options)["derating_scenario"] is None


def test_uncalibrated_outlook_has_no_derating_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    PlantConfiguration.objects.filter(plant=plant).update(created_at=NOW)
    publish_snapshot(snapshot_for(system, NOW.date(), collected_at=NOW))
    result = _outlook(system, plant)
    assert result["calibration_id"] is None
    assert result["derating_scenario"] is None


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
