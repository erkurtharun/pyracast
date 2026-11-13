from datetime import date, timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastResult, ForecastSnapshot, RequestAttempt
from pyracast.incidents.operations import save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import Plant, PlantConfiguration, PlantForecast
from pyracast.plants.operations import revise_plant
from pyracast.regions.models import Region
from tests.test_domain import System
from tests.test_plant_operations import make_plant, publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 11)


def reference_transition(
    monkeypatch: pytest.MonkeyPatch, *, archive_periods: int = 48, with_latest: bool = True
) -> tuple[System, Plant, int]:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    old = snapshot_for(system, DAY, power_kw=0.1)
    old.normalized_intervals = old.normalized_intervals[:archive_periods]
    publish_snapshot(old)
    monkeypatch.setattr(timezone, "now", lambda: start + timedelta(hours=20, minutes=5))
    new = SiteBinding.objects.get(pk=system.binding.pk)
    new.pk = None
    new.capacity_kw = 1000
    new.dc_capacity_kw = 1000
    new.save()
    Region.objects.filter(pk=system.region.pk).update(approved_binding=new)
    plant = revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name="Plant",
        dc_capacity_mw=130,
        export_limit_mw=100,
    )
    system.binding = new
    if with_latest:
        latest = snapshot_for(system, DAY, power_kw=200, collected_at=timezone.now())
        latest.normalized_intervals = latest.normalized_intervals[40:]
        publish_snapshot(latest)
    return system, plant, old.pk


def test_today_automatic_uses_complete_original_forecast_after_reference_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, old_id = reference_transition(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    current = plant_outlook(*scope, target_date=DAY, resolution_minutes=60, source="forecast")
    assert cast(dict[str, Any], current["result"])["coverage_fraction"] == pytest.approx(1 / 6)
    count = RequestAttempt.objects.count()
    automatic = plant_outlook(*scope, target_date=DAY, resolution_minutes=60, source="auto")
    result = cast(dict[str, Any], automatic["result"])
    assert automatic["source_kind"] == "forecast"
    assert result["complete"] is True
    assert result["coverage_fraction"] == 1
    assert result["energy_mwh"] == pytest.approx(312)
    assert len(result["intervals"]) == 24
    assert result["intervals"][12]["mean_power_mw"] == pytest.approx(13)
    assert result["provenance"]["snapshot_id"] == old_id
    assert automatic["configuration_id"] != plant.current_configuration_id
    assert "reference_transition_fallback" in result["quality_flags"]
    assert not PlantForecast.objects.exists()
    assert RequestAttempt.objects.count() == count
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    response = client.get(
        path, {"date": DAY.isoformat(), "resolution_minutes": "60", "data_source": "auto"}
    )
    assert response.status_code == 200
    assert response.json()["result"]["coverage_fraction"] == 1


def test_reference_fallback_keeps_current_known_outage_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = reference_transition(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    incident = save_incident(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        kind="panels",
        title="Current outage",
        notes="",
        starts_at=start + timedelta(hours=8),
        ends_at=None,
        available_dc_mw=65,
        available_inverter_mw=None,
        available_export_mw=None,
    )
    outlook = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="auto",
    )
    assert cast(dict[str, Any], outlook["result"])["complete"] is True
    scenario = cast(dict[str, Any], outlook["outage_scenario"])
    assert scenario["status"] == "unknown_impact"
    assert scenario["incidents"][0]["id"] == incident.pk
    assert scenario["evaluated_at"] == timezone.now().isoformat()
    assert "outage_configuration_changed" in scenario["result"]["quality_flags"]
    assert scenario["result"]["intervals"][12]["mean_power_mw"] is None


def test_past_automatic_keeps_missing_solcast_actuals_distinct_from_forecasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    day = timezone.now().date() - timedelta(days=1)
    publish_snapshot(snapshot_for(system, day))
    result = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=day,
        resolution_minutes=60,
        source="auto",
    )
    assert result["source_kind"] == "estimated_actuals"
    assert result["result"] is None
    assert result["unavailable_reason"] == "estimated_actuals_unavailable"


def test_reference_transition_before_first_new_acquisition_keeps_published_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, old_id = reference_transition(monkeypatch, with_latest=False)
    result = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="auto",
    )
    payload = cast(dict[str, Any], result["result"])
    assert payload["complete"] is True
    assert payload["provenance"]["snapshot_id"] == old_id


@pytest.mark.parametrize(
    "change",
    [
        "same_capacity",
        "unverified",
        "unhealthy",
        "physical",
        "location",
        "no_prior_configuration",
        "not_intraday",
        "unpublished",
        "invalid_archive",
    ],
)
def test_automatic_does_not_replace_latest_when_reference_transition_is_not_safe(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    system, plant, old_id = reference_transition(monkeypatch)
    assert plant.current_configuration_id is not None
    old = ForecastSnapshot.objects.select_related("job").get(pk=old_id)
    if change == "same_capacity":
        SiteBinding.objects.filter(pk=system.binding.pk).update(capacity_kw=1)
    elif change == "unverified":
        SiteBinding.objects.filter(pk=old.job.binding_id).update(capacity_semantics_verified=False)
    elif change == "unhealthy":
        SiteBinding.objects.filter(pk=old.job.binding_id).update(health="error")
    elif change == "physical":
        PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(
            dc_capacity_mw=260
        )
    elif change == "location":
        SiteBinding.objects.filter(pk=system.binding.pk).update(latitude=41)
    elif change == "no_prior_configuration":
        start, _ = local_day_bounds(DAY, system.binding.timezone)
        PlantConfiguration.objects.filter(plant=plant).exclude(
            pk=plant.current_configuration_id
        ).update(created_at=start + timedelta(hours=1))
    elif change == "not_intraday":
        PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(
            created_at=old.collected_at
        )
    elif change == "unpublished":
        ForecastResult.objects.filter(snapshot=old).delete()
    else:
        old.normalized_intervals[0]["power_kw"] = "invalid"
        old.save(update_fields=["normalized_intervals"])
    outlook = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="auto",
    )
    assert outlook["configuration_id"] == plant.current_configuration_id
    payload = cast(dict[str, Any], outlook["result"])
    assert payload["complete"] is False
    assert payload["coverage_fraction"] == pytest.approx(1 / 6)
    assert "reference_transition_fallback" not in payload["quality_flags"]


@pytest.mark.parametrize("cached_complete", [False, True])
def test_partial_archive_cannot_supply_a_complete_automatic_forecast(
    monkeypatch: pytest.MonkeyPatch,
    cached_complete: bool,
) -> None:
    system, plant, old_id = reference_transition(monkeypatch, archive_periods=24)
    if cached_complete:
        old = ForecastSnapshot.objects.select_related("job").get(pk=old_id)
        configuration = PlantConfiguration.objects.get(plant=plant, binding_id=old.job.binding_id)
        PlantForecast.objects.create(
            configuration=configuration,
            snapshot=old,
            calibration=None,
            target_date=DAY,
            resolution_minutes=60,
            payload={"complete": True, "quality_flags": [], "provenance": {"snapshot_id": old_id}},
        )
    outlook = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="auto",
    )
    assert outlook["configuration_id"] == plant.current_configuration_id
    payload = cast(dict[str, Any], outlook["result"])
    assert payload["complete"] is False
    assert payload["coverage_fraction"] == pytest.approx(1 / 6)


def test_new_reference_internal_missing_values_remain_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = reference_transition(monkeypatch)
    source = snapshot_for(
        system, DAY, power_kw=200, collected_at=timezone.now() - timedelta(seconds=1)
    )
    source.normalized_intervals[24]["power_kw"] = None
    publish_snapshot(source)
    outlook = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="auto",
    )
    assert outlook["configuration_id"] == plant.current_configuration_id
    payload = cast(dict[str, Any], outlook["result"])
    assert payload["complete"] is False
    assert payload["coverage_fraction"] == pytest.approx(47 / 48)
    assert payload["intervals"][12]["mean_power_mw"] is None
    assert "reference_transition_fallback" not in payload["quality_flags"]
