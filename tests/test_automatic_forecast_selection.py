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
from pyracast.plants.models import ForecastIssuance, PlantConfiguration
from tests.support.automatic_forecast_selection import DAY, reference_transition
from tests.support.plant_operations import make_plant, publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


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


def test_past_automatic_labels_saved_forecast_when_solcast_actuals_are_missing(
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
    assert result["source_kind"] == "forecast"
    payload = cast(dict[str, Any], result["result"])
    assert payload["provenance"]["fallback_reason"] == "estimated_actuals_unavailable"
    assert result["unavailable_reason"] is None


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
        ForecastIssuance.objects.filter(snapshot=old).delete()
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


def test_partial_archive_cannot_supply_a_complete_automatic_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = reference_transition(monkeypatch, archive_periods=24)
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


def test_reference_transition_selects_archive_for_the_previous_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, original_snapshot_id = reference_transition(monkeypatch)
    current = plant.current_configuration
    assert current is not None
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    binding = SiteBinding.objects.get(pk=current.binding_id)
    binding.pk = None
    binding.region_revision += 1
    binding.site_id = "intermediate-site"
    binding.save()
    PlantConfiguration.objects.filter(pk=current.pk).update(revision=3)
    intermediate = PlantConfiguration.objects.create(
        plant=plant,
        region=plant.region,
        binding=binding,
        revision=2,
        dc_capacity_mw=130,
        export_limit_mw=100,
        measurement_basis="net_export",
        created_by=system.member,
        created_at=start + timedelta(hours=1),
    )
    PlantConfiguration.objects.filter(pk=intermediate.pk).update(
        created_at=start + timedelta(hours=1)
    )
    plant.current_configuration = intermediate
    plant.save(update_fields=["current_configuration"])
    current_binding = system.binding
    system.binding = binding
    acquired = start + timedelta(hours=3)
    source = publish_snapshot(snapshot_for(system, DAY, power_kw=100, collected_at=acquired))
    assert ForecastIssuance.objects.filter(snapshot=source).exists()
    plant.current_configuration = current
    plant.save(update_fields=["current_configuration"])
    system.binding = current_binding
    outlook = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="auto",
    )
    payload = cast(dict[str, Any], outlook["result"])
    assert payload["provenance"]["snapshot_id"] == original_snapshot_id
    assert "reference_transition_fallback" in payload["quality_flags"]


def test_reference_fallback_drops_invalidated_calibration_without_changing_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, source_id = reference_transition(monkeypatch, calibrated=True)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    archived = plant_outlook(
        *scope, target_date=DAY, resolution_minutes=60, source="archived_forecast"
    )
    original = cast(dict[str, Any], archived["result"])
    assert archived["calibration_id"] is not None
    assert original["factor"] == 0.5
    assert original["energy_mwh"] == pytest.approx(156)
    issuance = ForecastIssuance.objects.get(snapshot_id=source_id)
    payloads_before = issuance.payloads
    training_start, _ = local_day_bounds(date(2026, 11, 4), system.binding.timezone)
    save_incident(
        *scope,
        kind="panels",
        title="Late reported training outage",
        notes="",
        starts_at=training_start + timedelta(hours=9),
        ends_at=training_start + timedelta(hours=10),
        available_dc_mw=65,
        available_inverter_mw=None,
        available_export_mw=None,
    )
    live = plant_outlook(*scope, target_date=DAY, resolution_minutes=60, source="auto")
    normal = cast(dict[str, Any], live["result"])
    assert live["configuration_id"] == archived["configuration_id"]
    assert live["calibration_id"] is None
    assert live["outage_scenario"] is None
    assert normal["factor"] == 1
    assert normal["energy_mwh"] == pytest.approx(312)
    assert normal["provenance"]["snapshot_id"] == source_id
    assert "source_segments" not in normal["provenance"]
    assert "calibration_invalidated_by_outage_history" in normal["quality_flags"]
    replay = plant_outlook(
        *scope, target_date=DAY, resolution_minutes=60, source="archived_forecast"
    )
    assert replay == archived
    issuance.refresh_from_db()
    assert issuance.payloads == payloads_before
