from datetime import date, datetime, timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from pyracast.forecasts.models import RequestAttempt
from pyracast.incidents.operations import save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.plants.activation import activate_calibration
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import PlantConfiguration
from tests.support.domain import NOW
from tests.support.plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 11)
CURRENT = NOW + timedelta(days=2)


@pytest.mark.parametrize("resolution", [30, 60])
def test_intraday_update_keeps_archived_morning_in_current_outlook_and_api(
    monkeypatch: pytest.MonkeyPatch, resolution: int
) -> None:
    system, plant = make_plant(monkeypatch)
    start, end = local_day_bounds(DAY, system.binding.timezone)
    PlantConfiguration.objects.filter(plant=plant).update(
        created_at=start + timedelta(hours=10, minutes=30)
    )
    old = publish_snapshot(
        snapshot_for(system, DAY, power_kw=0.1, collected_at=CURRENT - timedelta(hours=31))
    )
    latest = snapshot_for(
        system,
        DAY,
        power_kw=0.2,
        collected_at=CURRENT - timedelta(hours=5),
        issued_at=CURRENT - timedelta(hours=5),
    )
    latest.normalized_intervals = latest.normalized_intervals[21:]
    latest.job.target_date = DAY + timedelta(days=1)
    latest.job.save(update_fields=["target_date"])
    publish_snapshot(latest)
    monkeypatch.setattr(timezone, "now", lambda: CURRENT)
    requests = RequestAttempt.objects.count()
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    outlook = plant_outlook(*scope, target_date=DAY, resolution_minutes=resolution)
    payload = cast(dict[str, Any], outlook["result"])
    assert payload["energy_mwh"] == pytest.approx(487.5)
    assert outlook["freshness"] == "stale"
    assert len(payload["intervals"]) == 1440 // resolution
    assert payload["intervals"][0]["mean_power_mw"] == pytest.approx(13)
    assert payload["intervals"][-1]["mean_power_mw"] == pytest.approx(26)
    assert payload["intervals"][0]["source_snapshot_ids"] == [old.pk]
    assert payload["intervals"][-1]["source_snapshot_ids"] == [latest.pk]
    transition = payload["intervals"][600 // resolution]
    assert transition["mean_power_mw"] == pytest.approx(13 if resolution == 30 else 19.5)
    assert transition["source_snapshot_ids"] == (
        [old.pk] if resolution == 30 else [old.pk, latest.pk]
    )
    assert payload["provenance"]["snapshot_id"] == latest.pk
    assert payload["provenance"]["selection_policy"] == "latest-forecast-coverage-v2"
    assert payload["provenance"]["source_snapshot_ids"] == [latest.pk, old.pk]
    assert payload["provenance"]["source_snapshots"] == [
        {
            "snapshot_id": snapshot.pk,
            "collected_at": snapshot.collected_at.isoformat(),
            "available_at": snapshot.collected_at.isoformat(),
            "issued_at": snapshot.issued_at.isoformat() if snapshot.issued_at else None,
            "coverage_start": snapshot.normalized_intervals[0]["start"],
            "coverage_end": snapshot.normalized_intervals[-1]["end"],
        }
        for snapshot in (latest, old)
    ]
    boundary = start + timedelta(hours=10, minutes=30)
    assert payload["provenance"]["source_segments"] == [
        {"snapshot_id": old.pk, "start": start.isoformat(), "end": boundary.isoformat()},
        {"snapshot_id": latest.pk, "start": boundary.isoformat(), "end": end.isoformat()},
    ]
    assert "mixed_forecast_vintages" in payload["quality_flags"]
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    response = client.get(
        path,
        {"date": DAY.isoformat(), "resolution_minutes": str(resolution), "data_source": "forecast"},
    )
    assert response.status_code == 200, response.content
    assert response.json()["result"] == payload
    irradiance = response.json()["regional_irradiance"]
    assert irradiance["vintage"]["scope"] == "primary_source"
    assert irradiance["provenance"]["source_snapshots"] == payload["provenance"]["source_snapshots"]
    assert irradiance["provenance"]["source_segments"] == payload["provenance"]["source_segments"]
    assert irradiance["intervals"][20]["source_snapshot_ids"] == [old.pk]
    assert irradiance["intervals"][21]["source_snapshot_ids"] == [latest.pk]
    assert "mixed_forecast_vintages" in irradiance["quality_flags"]
    assert RequestAttempt.objects.count() == requests


def test_older_coverage_does_not_replace_new_null_gap_or_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    old = publish_snapshot(snapshot_for(system, DAY, power_kw=0.1))
    latest = snapshot_for(system, DAY, power_kw=0.2, collected_at=CURRENT - timedelta(hours=5))
    latest.normalized_intervals = latest.normalized_intervals[21:]
    latest.normalized_intervals[1]["power_kw"] = None
    latest.normalized_intervals[3]["power_kw"] = 0
    del latest.normalized_intervals[2]
    publish_snapshot(latest)
    monkeypatch.setattr(timezone, "now", lambda: CURRENT)
    outlook = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=30,
    )
    payload = cast(dict[str, Any], outlook["result"])
    rows = payload["intervals"]
    assert rows[20]["mean_power_mw"] == pytest.approx(13)
    assert rows[20]["source_snapshot_ids"] == [old.pk]
    assert rows[21]["mean_power_mw"] == pytest.approx(26)
    assert rows[22]["mean_power_mw"] is None
    assert rows[23]["mean_power_mw"] is None
    assert rows[24]["mean_power_mw"] == 0
    assert rows[22]["source_snapshot_ids"] == [latest.pk]
    assert rows[23]["source_snapshot_ids"] == [latest.pk]
    assert rows[24]["source_snapshot_ids"] == [latest.pk]
    assert payload["energy_mwh"] is None
    assert payload["partial_energy_mwh"] == pytest.approx(448.5)


def test_calibrated_power_is_clipped_before_aggregating_source_ownership_pieces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    calibration = ready_run(system, plant)
    activate_calibration(*scope, calibration.pk)
    old = publish_snapshot(snapshot_for(system, DAY, power_kw=2))
    latest = snapshot_for(system, DAY, power_kw=0.4, collected_at=CURRENT - timedelta(hours=5))
    latest.normalized_intervals = latest.normalized_intervals[20:]
    boundary = datetime.fromisoformat(latest.normalized_intervals[0]["start"]) + timedelta(
        minutes=15
    )
    latest.normalized_intervals[0]["start"] = boundary.isoformat()
    publish_snapshot(latest)
    monkeypatch.setattr(timezone, "now", lambda: CURRENT)
    outlook = plant_outlook(*scope, target_date=DAY, resolution_minutes=60)
    payload = cast(dict[str, Any], outlook["result"])
    assert outlook["calibration_id"] == calibration.pk
    assert payload["factor"] == 0.5
    assert payload["intervals"][9]["mean_power_mw"] == pytest.approx(100)
    assert payload["intervals"][10]["mean_power_mw"] == pytest.approx(44.5)
    assert payload["intervals"][10]["source_snapshot_ids"] == [old.pk, latest.pk]
    assert payload["intervals"][11]["mean_power_mw"] == pytest.approx(26)
    assert payload["energy_mwh"] == pytest.approx(1382.5)
    assert "uniform_mean_proration" in payload["quality_flags"]
    irradiance = cast(dict[str, Any], outlook["regional_irradiance"])
    assert "uniform_mean_proration" in irradiance["quality_flags"]
    assert irradiance["provenance"]["source_intervals"] == payload["provenance"]["source_intervals"]
    assert payload["provenance"]["source_intervals"][20] == {
        "snapshot_id": old.pk,
        "start": (boundary - timedelta(minutes=15)).isoformat(),
        "end": boundary.isoformat(),
        "original_start": (boundary - timedelta(minutes=15)).isoformat(),
        "original_end": (boundary + timedelta(minutes=15)).isoformat(),
    }


@pytest.mark.parametrize("calibrated", [False, True])
def test_outage_scenario_and_dropped_calibration_keep_composite_morning(
    monkeypatch: pytest.MonkeyPatch, calibrated: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    if calibrated:
        calibration = ready_run(system, plant)
        activate_calibration(*scope, calibration.pk)
    old = publish_snapshot(snapshot_for(system, DAY, power_kw=0.1))
    latest = snapshot_for(system, DAY, power_kw=0.2, collected_at=CURRENT - timedelta(hours=5))
    latest.normalized_intervals = latest.normalized_intervals[21:]
    publish_snapshot(latest)
    monkeypatch.setattr(timezone, "now", lambda: CURRENT)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    save_incident(
        *scope,
        kind="panels",
        title="Reduced array",
        notes="",
        starts_at=start + timedelta(hours=12),
        ends_at=None,
        available_dc_mw=65,
        available_inverter_mw=None,
        available_export_mw=None,
    )
    if calibrated:
        training_start, _ = local_day_bounds(date(2026, 11, 4), system.binding.timezone)
        save_incident(
            *scope,
            kind="panels",
            title="Corrected training outage",
            notes="",
            starts_at=training_start + timedelta(hours=9),
            ends_at=training_start + timedelta(hours=10),
            available_dc_mw=65,
            available_inverter_mw=None,
            available_export_mw=None,
        )
    outlook = plant_outlook(*scope, target_date=DAY, resolution_minutes=60)
    normal = cast(dict[str, Any], outlook["result"])
    affected = cast(dict[str, Any], outlook["outage_scenario"])["result"]
    assert normal["energy_mwh"] == pytest.approx(487.5)
    assert normal["factor"] == 1
    assert normal["intervals"][9]["mean_power_mw"] == pytest.approx(13)
    assert affected["intervals"][9]["mean_power_mw"] == pytest.approx(13)
    assert affected["intervals"][9]["source_snapshot_ids"] == [old.pk]
    assert affected["intervals"][13]["mean_power_mw"] == pytest.approx(13)
    assert affected["intervals"][13]["source_snapshot_ids"] == [latest.pk]
    assert affected["energy_mwh"] == pytest.approx(331.5)
    assert affected["daylight"]["complete"] is normal["daylight"]["complete"] is True
    assert affected["daylight"]["windows"] == normal["daylight"]["windows"]
    assert 0 < affected["daylight"]["energy_mwh"] < normal["daylight"]["energy_mwh"]
    assert affected["provenance"]["selection_policy"] == "latest-forecast-coverage-v2"
    for key in ("source_snapshots", "source_segments", "source_intervals"):
        assert affected["provenance"][key] == normal["provenance"][key]
    if calibrated:
        assert outlook["calibration_id"] is None
        assert "uncalibrated" in normal["quality_flags"]
        assert normal["provenance"]["calibration_assessment"] == "uncalibrated"


def test_a_chosen_forecast_stays_on_its_saved_record_without_vintage_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    first = publish_snapshot(snapshot_for(system, DAY, power_kw=0.1))
    latest = snapshot_for(system, DAY, power_kw=0.2, collected_at=CURRENT - timedelta(hours=5))
    latest.normalized_intervals = latest.normalized_intervals[21:]
    publish_snapshot(latest)
    monkeypatch.setattr(timezone, "now", lambda: CURRENT)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    replay = plant_outlook(*scope, target_date=DAY, resolution_minutes=60, forecast_id=first.pk)
    payload = cast(dict[str, Any], replay["result"])
    assert payload["energy_mwh"] == pytest.approx(312)
    assert payload["provenance"]["snapshot_id"] == first.pk
    assert payload["provenance"]["selection_policy"] == "latest-published-pre-daylight-issuance-v1"
    assert "mixed_forecast_vintages" not in payload["quality_flags"]
