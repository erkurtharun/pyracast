from datetime import date, datetime, timedelta
from typing import Any, cast

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from pyracast.incidents.models import PlantIncident
from pyracast.incidents.operations import finish_incident, save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.plants import training
from pyracast.plants.calibration_data import build_calibration_dataset
from pyracast.plants.history import plant_outlook
from pyracast.plants.incident_policy import incidents_unchanged
from pyracast.plants.measurements import select_measurements
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    ForecastIssuance,
    Plant,
    PlantForecast,
    ProductionObservation,
)
from pyracast.plants.numerics import FitResult, fit_calibration
from pyracast.plants.operations import activate_calibration, revise_plant
from tests.test_domain import NOW, System
from tests.test_plant_operations import (
    make_plant,
    publish_snapshot,
    ready_run,
    request_run,
    snapshot_for,
)
from tests.test_plant_training import actual_snapshot_for, history

pytestmark = pytest.mark.django_db(transaction=True)


def incident(
    system: System, plant: Plant, start: datetime, end: datetime | None, **caps: Any
) -> PlantIncident:
    return save_incident(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        kind="inverter",
        title="Inverter outage",
        notes="",
        starts_at=start,
        ends_at=end,
        **(
            {"available_dc_mw": 65, "available_inverter_mw": None, "available_export_mw": None}
            | caps
        ),
    )


def test_late_outage_supersedes_fit_preserves_meter_readings_and_cancel_restores_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    history(system, plant)
    pending = request_run(system, plant)
    config = plant.current_configuration
    assert config is not None
    start, end = local_day_bounds(date(2026, 11, 4), system.binding.timezone)
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(seconds=10))
    outage = incident(
        system,
        plant,
        start + timedelta(hours=9, minutes=15),
        start + timedelta(hours=9, minutes=45),
    )
    pending.refresh_from_db()
    assert pending.status == "superseded"
    diagnostic = CalibrationRun.objects.get(status="pending")
    assert diagnostic.pk != pending.pk
    assert diagnostic.incident_revision_ids == [outage.revisions.get().pk]
    assert training.run_calibration(diagnostic.pk, timezone.now())
    diagnostic.refresh_from_db()
    assert diagnostic.status == "insufficient"
    assert diagnostic.factor is None
    assert diagnostic.result["valid_days"] == 6
    assert diagnostic.result["included_day_dates"] == [
        f"2026-11-{day:02d}" for day in (2, 3, 5, 6, 7, 8)
    ]
    assert not CalibrationActivation.objects.exists()
    rows, _ = select_measurements(config, start, end, timezone.now())
    assert sum(row.quality == "outage" for row in rows) == 1
    assert not ProductionObservation.objects.exclude(quality="valid").exists()
    old_rows, _ = select_measurements(
        config, start, end, pending.cutoff_at, incident_revision_ids=[]
    )
    assert all(row.quality == "valid" for row in old_rows)
    assert not incidents_unchanged(pending, timezone.now())
    finish_incident(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        incident_id=outage.pk,
        expected_revision=1,
        cancel=True,
    )
    replacement = CalibrationRun.objects.get(status="pending")
    assert replacement.pk != pending.pk
    assert replacement.pk != diagnostic.pk
    assert replacement.incident_revision_ids == []
    assert training.run_calibration(replacement.pk, timezone.now())
    replacement.refresh_from_db()
    assert replacement.status == "ready"
    assert replacement.result["valid_days"] == 7
    assert replacement.result["included_day_dates"] == [f"2026-11-{day:02d}" for day in range(2, 9)]
    assert not ProductionObservation.objects.exclude(quality="valid").exists()
    assert not CalibrationActivation.objects.exists()


def test_outage_recorded_while_fitting_prevents_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    history(system, plant)
    run = request_run(system, plant)
    start, _ = local_day_bounds(date(2026, 11, 4), system.binding.timezone)

    def changed(*args: Any, **kwargs: Any) -> FitResult:
        monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(seconds=10))
        incident(system, plant, start + timedelta(hours=9), start + timedelta(hours=10))
        return fit_calibration(*args, **kwargs)

    monkeypatch.setattr(training, "fit_calibration", changed)
    assert not training.run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "superseded"
    assert run.dataset_manifest == {}


def test_fit_pins_night_outage_and_ignores_changes_outside_its_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    history(system, plant)
    start, _ = local_day_bounds(date(2026, 11, 4), system.binding.timezone)
    night = incident(system, plant, start, start + timedelta(hours=1))
    run = request_run(system, plant)
    evidence_id = night.revisions.get().pk
    assert run.incident_revision_ids == [evidence_id]
    incident(system, plant, NOW - timedelta(minutes=1), None)
    run.refresh_from_db()
    assert run.status == "pending"
    assert incidents_unchanged(run, NOW)
    assert training.run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "ready"
    assert run.dataset_manifest["incident_revision_ids"] == [evidence_id]
    assert run.dataset_manifest["incident_policy"] == "exclude-whole-overlapping-meter-interval-v1"
    assert run.result["selection"]["unmatched_days"] == []


def test_live_outlook_rejects_contaminated_factor_without_rewriting_saved_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    ready = ready_run(system, plant)
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, ready.pk)
    target = date(2026, 11, 10)
    publish_snapshot(snapshot_for(system, target, collected_at=NOW))
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    original = plant_outlook(*scope, target_date=target, resolution_minutes=60)
    archived = plant_outlook(*scope, target_date=target, resolution_minutes=60, as_of=NOW)
    saved = ForecastIssuance.objects.get()
    frozen_payloads = saved.payloads
    assert cast(dict[str, Any], original["result"])["factor"] == 0.5
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=1))
    start, _ = local_day_bounds(date(2026, 11, 4), system.binding.timezone)
    incident(system, plant, start + timedelta(hours=9), start + timedelta(hours=10))
    current = plant_outlook(*scope, target_date=target, resolution_minutes=60)
    assert cast(dict[str, Any], current["result"])["factor"] == 1
    assert current["calibration_id"] is None
    assert "uncalibrated" in cast(dict[str, Any], current["result"])["quality_flags"]
    saved.refresh_from_db()
    assert saved.payloads == frozen_payloads
    historical = plant_outlook(*scope, target_date=target, resolution_minutes=60, as_of=NOW)
    assert cast(dict[str, Any], historical["result"])["factor"] == 0.5
    with pytest.raises(ValidationError, match="outage history changed"):
        activate_calibration(*scope, ready.pk)
    new_run = request_run(system, plant)
    assert build_calibration_dataset(new_run, new_run.configuration).incumbent_factor == 1
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=2))
    past = plant_outlook(*scope, target_date=target, resolution_minutes=60)
    past_payload = cast(dict[str, Any], past["result"])
    assert past_payload["factor"] == 0.5
    assert past["calibration_id"] == ready.pk
    assert "calibration_invalidated_by_outage_history" not in past_payload["quality_flags"]
    assert past_payload == archived["result"]
    saved.refresh_from_db()
    assert saved.payloads == frozen_payloads
    assert not PlantForecast.objects.exists()


def test_outlook_shows_partial_outage_then_single_forecast_after_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    day = date(2026, 11, 9)
    start, _ = local_day_bounds(day, system.binding.timezone)
    publish_snapshot(snapshot_for(system, day))
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    outage = incident(system, plant, start + timedelta(hours=9, minutes=15), None)
    outlook = plant_outlook(*scope, target_date=day, resolution_minutes=60)
    scenario = cast(dict[str, Any], outlook["outage_scenario"])
    assert scenario["status"] == "estimated"
    assert scenario["result"]["intervals"][9]["mean_power_mw"] == 32.5
    assert scenario["result"]["intervals"][10]["mean_power_mw"] == 26
    finish_incident(
        *scope,
        incident_id=outage.pk,
        expected_revision=1,
        ends_at=start + timedelta(hours=10, minutes=15),
    )
    assert plant_outlook(*scope, target_date=day, resolution_minutes=60)["outage_scenario"] is None
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=1))
    actual_snapshot_for(system, day)
    retrospective = plant_outlook(
        *scope, target_date=day, resolution_minutes=60, source="estimated_actuals"
    )
    assert cast(dict[str, Any], retrospective["outage_scenario"])["result"]["energy_mwh"] == 1222


def test_unknown_or_changed_configuration_never_invents_affected_power(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    day = date(2026, 11, 10)
    publish_snapshot(snapshot_for(system, day, collected_at=NOW))
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    incident(system, plant, NOW - timedelta(hours=1), None, available_dc_mw=None)
    unknown = plant_outlook(*scope, target_date=day, resolution_minutes=30)
    scenario = cast(dict[str, Any], unknown["outage_scenario"])
    assert scenario["status"] == "unknown_impact"
    assert scenario["result"]["energy_mwh"] is None
    assert scenario["result"]["intervals"][20]["mean_power_mw"] is None
    revise_plant(*scope, name="Rebuilt", dc_capacity_mw=100, export_limit_mw=80)
    changed = plant_outlook(*scope, target_date=day, resolution_minutes=30)
    assert (
        "outage_configuration_changed"
        in cast(dict[str, Any], changed["outage_scenario"])["result"]["quality_flags"]
    )
