from copy import deepcopy
from datetime import date, timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.incidents.models import IncidentRevision, PlantIncident
from pyracast.incidents.operations import save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.notifications.report_data import saved_report_context
from pyracast.plants.activation import activate_calibration
from pyracast.plants.forecast_archive import prepare_forecast_issuances
from pyracast.plants.history import plant_outlook
from pyracast.plants.incident_forecasts import incident_scenario
from pyracast.plants.models import CalibrationActivation, Plant
from pyracast.plants.operations import revise_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.models import Region
from tests.support.concurrency import row_locked_elsewhere, short_lock_timeout
from tests.support.domain import NOW, System
from tests.support.incident_calibration import incident
from tests.support.notification_report_data import report_delivery
from tests.support.notifications import SLOT
from tests.support.plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for
from tests.support.plant_training import actual_snapshot_for
from tests.support.trash_operations import sibling_plant

pytestmark = pytest.mark.django_db(transaction=True)


def outages_url(system: System, plant: Plant, region_id: int | None = None) -> str:
    region = system.region.pk if region_id is None else region_id
    return f"/api/tenants/{system.tenant.pk}/regions/{region}/plants/{plant.pk}/outages/"


def admin(system: System) -> Client:
    client = Client()
    client.force_login(system.member)
    return client


def test_resolved_total_outage_keeps_zero_power_after_rename_and_tilt_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    day = date(2026, 11, 9)
    start, _ = local_day_bounds(day, system.binding.timezone)
    publish_snapshot(snapshot_for(system, day))
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    declared = plant.current_configuration_id
    incident(
        system, plant, start + timedelta(hours=9), start + timedelta(hours=10), available_dc_mw=0
    )
    edited = revise_plant(
        *scope,
        PlantParameters(
            name="Renamed",
            dc_capacity_mw=130,
            export_limit_mw=100,
            mounting_type="fixed",
            surface_tilt_degrees=25,
            surface_azimuth_degrees=180,
        ),
    )
    assert edited.current_configuration_id != declared
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=1))
    actual_snapshot_for(system, day)
    outlook = plant_outlook(
        *scope, target_date=day, resolution_minutes=60, source="estimated_actuals"
    )
    assert outlook["configuration_id"] == edited.current_configuration_id
    scenario = cast(dict[str, Any], outlook["outage_scenario"])
    assert scenario["status"] == "estimated"
    assert scenario["result"]["intervals"][9]["mean_power_mw"] == 0
    assert scenario["result"]["intervals"][10]["mean_power_mw"] > 0
    assert "unknown_outage_impact" not in scenario["result"]["quality_flags"]
    assert "outage_configuration_changed" not in scenario["result"]["quality_flags"]
    assert scenario["result"]["provenance"]["scenario_policy"] == "reported-operating-limits-v2"


def test_reopening_a_resolved_outage_declares_and_validates_the_current_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    start = NOW - timedelta(hours=3)
    outage = incident(system, plant, start, start + timedelta(hours=1), available_dc_mw=100)
    resolved_under = outage.declared_configuration_id
    rebuilt = revise_plant(
        *scope, PlantParameters(name="Plant", dc_capacity_mw=80, export_limit_mw=70)
    )
    client = admin(system)
    detail = f"{outages_url(system, plant)}{outage.pk}/"
    payload = {
        "kind": "inverter",
        "title": "Inverter outage",
        "starts_at": start.isoformat(),
        "ends_at": None,
        "expected_revision": 1,
    }
    rejected = client.put(
        detail, payload | {"available_dc_mw": 100}, content_type="application/json"
    )
    assert rejected.status_code == 400, rejected.content
    assert rejected.json()["code"] == "incident_capacity_exceeded"
    reopened = client.put(
        detail, payload | {"available_dc_mw": 60}, content_type="application/json"
    )
    assert reopened.status_code == 200, reopened.content
    assert reopened.json()["status"] == "active"
    assert reopened.json()["declared_configuration_id"] == rebuilt.current_configuration_id
    assert reopened.json()["declared_configuration_id"] != resolved_under


@pytest.mark.parametrize("action", ["update", "resolve", "cancel"])
def test_outage_of_another_plant_or_tenant_answers_404_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    system, plant = make_plant(monkeypatch)
    start = NOW - timedelta(hours=2)
    neighbour_plant, foreign_plant = sibling_plant(system), sibling_plant(system)
    neighbour, foreign = (
        save_incident(
            system.member,
            system.tenant.pk,
            target.region_id,
            target.pk,
            kind="other",
            title="Elsewhere",
            notes="",
            starts_at=start,
            ends_at=None,
            available_dc_mw=None,
            available_inverter_mw=None,
            available_export_mw=None,
        )
        for target in (neighbour_plant, foreign_plant)
    )
    Region.objects.filter(pk=foreign_plant.region_id).update(
        tenant=Tenant.objects.create(name="Foreign")
    )
    client = admin(system)
    for target in (neighbour, foreign):
        detail = f"{outages_url(system, plant)}{target.pk}/"
        if action == "update":
            response = client.put(
                detail,
                {
                    "kind": "other",
                    "title": "Taken over",
                    "starts_at": start.isoformat(),
                    "expected_revision": 1,
                },
                content_type="application/json",
            )
        else:
            body: dict[str, Any] = {"expected_revision": 1}
            if action == "resolve":
                body["ends_at"] = (start + timedelta(hours=1)).isoformat()
            response = client.post(f"{detail}{action}/", body, content_type="application/json")
        assert response.status_code == 404, response.content
        target.refresh_from_db()
        assert target.revision == 1
        assert target.ends_at is None
        assert target.cancelled_at is None
        assert IncidentRevision.objects.filter(incident=target).count() == 1


@pytest.mark.parametrize("model", [Region, Plant])
def test_outage_write_behind_a_held_lock_answers_busy_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch, model: type[Region] | type[Plant]
) -> None:
    system, plant = make_plant(monkeypatch)
    client = admin(system)
    payload = {
        "kind": "inverter",
        "title": "Inverter",
        "starts_at": (NOW - timedelta(hours=1)).isoformat(),
    }
    pk = system.region.pk if model is Region else plant.pk
    with row_locked_elsewhere(model, pk), short_lock_timeout():
        response = client.post(outages_url(system, plant), payload, content_type="application/json")
    assert response.status_code == 503, response.content
    assert response.json()["code"] == "busy"
    assert response.headers["Retry-After"]
    assert not PlantIncident.objects.exists()


def test_invalidated_calibration_and_active_outage_through_the_live_outlook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    ready = ready_run(system, plant)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    activate_calibration(*scope, ready.pk)
    target = date(2026, 11, 10)
    publish_snapshot(snapshot_for(system, target, collected_at=NOW))
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=1))
    history_start, _ = local_day_bounds(date(2026, 11, 4), system.binding.timezone)
    incident(system, plant, history_start + timedelta(hours=9), history_start + timedelta(hours=10))
    day_start, _ = local_day_bounds(target, system.binding.timezone)
    active = incident(system, plant, day_start + timedelta(hours=8), None)
    outlook = plant_outlook(*scope, target_date=target, resolution_minutes=60)
    normal = cast(dict[str, Any], outlook["result"])
    scenario = cast(dict[str, Any], outlook["outage_scenario"])
    assert outlook["calibration_id"] is None
    assert normal["factor"] == 1
    assert "uncalibrated" in normal["quality_flags"]
    assert "calibration_invalidated_by_outage_history" not in normal["quality_flags"]
    assert scenario["status"] == "estimated"
    assert scenario["incidents"][0]["id"] == active.pk
    assert scenario["result"]["factor"] == 1
    assert scenario["result"]["energy_mwh"] < normal["energy_mwh"]
    assert "uncalibrated" in scenario["result"]["quality_flags"]
    assert scenario["result"]["provenance"]["snapshot_id"] == normal["provenance"]["snapshot_id"]


def test_scenario_of_a_calibrated_outlook_replaces_it_after_outage_history_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    ready = ready_run(system, plant)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    activate_calibration(*scope, ready.pk)
    target = date(2026, 11, 10)
    publish_snapshot(snapshot_for(system, target, collected_at=NOW))
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=1))
    outlook = plant_outlook(*scope, target_date=target, resolution_minutes=60)
    assert outlook["calibration_id"] == ready.pk
    before = deepcopy(outlook)
    history_start, _ = local_day_bounds(date(2026, 11, 4), system.binding.timezone)
    incident(system, plant, history_start + timedelta(hours=9), history_start + timedelta(hours=10))
    evaluated = incident_scenario(outlook, target, 60, timezone.now())
    assert outlook == before
    assert evaluated.scenario is None
    replacement = cast(dict[str, Any], evaluated.replacement_result)
    assert replacement["factor"] == 1
    assert "calibration_invalidated_by_outage_history" in replacement["quality_flags"]
    normal = cast(dict[str, Any], outlook["result"])
    assert replacement["provenance"]["snapshot_id"] == normal["provenance"]["snapshot_id"]


def test_invalidated_calibration_and_active_outage_through_the_saved_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, delivery = report_delivery(monkeypatch)
    run = ready_run(system, plant)
    assert plant.current_configuration is not None
    CalibrationActivation.objects.create(
        plant=plant, configuration=plant.current_configuration, calibration=run
    )
    monkeypatch.setattr(timezone, "now", lambda: SLOT - timedelta(minutes=1))
    window, _ = local_day_bounds(run.window_end, system.binding.timezone)
    incident(system, plant, window + timedelta(hours=9), window + timedelta(hours=10))
    outage = incident(system, plant, SLOT - timedelta(hours=2), None)
    tomorrow = cast(list[dict[str, Any]], saved_report_context(delivery)["days"])[1]
    assert tomorrow["normal"]["provenance"]["calibration_id"] is None
    assert tomorrow["normal"]["energy_mwh"] == pytest.approx(936)
    assert "uncalibrated" in tomorrow["normal"]["quality_flags"]
    assert tomorrow["outage_status"] == "estimated"
    assert tomorrow["outage"]["energy_mwh"] == pytest.approx(468)
    assert tomorrow["incident_revision_ids"] == [outage.revisions.get().pk]


def test_invalidated_calibration_and_active_outage_through_the_forecast_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = ready_run(system, plant)
    run.created_at = run.completed_at = run.cutoff_at = NOW - timedelta(hours=3)
    run.save()
    monkeypatch.setattr(timezone, "now", lambda: NOW - timedelta(hours=2))
    CalibrationActivation.objects.create(
        plant=plant, configuration=run.configuration, calibration=run
    )
    monkeypatch.setattr(timezone, "now", lambda: NOW - timedelta(hours=1))
    window, _ = local_day_bounds(run.window_end, system.binding.timezone)
    incident(system, plant, window + timedelta(hours=9), window + timedelta(hours=10))
    active = incident(system, plant, NOW - timedelta(minutes=90), None)
    day = date(2026, 11, 10)
    prepared = {
        item.target_date: item
        for item in prepare_forecast_issuances(snapshot_for(system, day, collected_at=NOW))
    }[day]
    assert prepared.calibration_id is None
    assert prepared.payloads["60"]["factor"] == 1
    scenario = cast(dict[str, Any], prepared.outage_scenarios["60"])
    assert scenario["status"] == "estimated"
    assert scenario["incidents"][0]["id"] == active.pk
    assert scenario["result"]["factor"] == 1
    assert scenario["result"]["energy_mwh"] < prepared.payloads["60"]["energy_mwh"]
    assert "calibration_invalidated_by_outage_history" not in scenario["result"]["quality_flags"]
