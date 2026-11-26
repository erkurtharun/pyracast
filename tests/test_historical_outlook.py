from datetime import date, datetime, timedelta
from typing import Any, cast

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.history import HISTORY_SELECTION_POLICY, select_actual_snapshot
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import ForecastIssuance, PlantConfiguration
from pyracast.plants.operations import revise_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.models import Region
from tests.support.domain import NOW, System
from tests.support.errors import raises_code
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant, publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 8)


def test_later_pre_sunrise_forecast_replaces_previous_evening_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    previous = publish_snapshot(
        snapshot_for(system, DAY, power_kw=0.2, collected_at=start - timedelta(hours=7))
    )
    latest = publish_snapshot(
        snapshot_for(system, DAY, power_kw=0.4, collected_at=start + timedelta(hours=3))
    )
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    before = plant_outlook(*args, target_date=DAY, resolution_minutes=60, forecast_id=previous.pk)
    after = plant_outlook(*args, target_date=DAY, resolution_minutes=60)
    assert before["snapshot_id"] == previous.pk
    assert after["snapshot_id"] == latest.pk
    assert after["result"] != before["result"]


def test_new_configuration_cannot_reconstruct_a_historical_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    PlantConfiguration.objects.filter(plant=plant).update(created_at=NOW)
    publish_snapshot(snapshot_for(system, DAY))
    result = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    assert result["result"] is None
    assert result["unavailable_reason"] == "configuration_unavailable"
    assert result["outage_scenario"] is None


def test_satellite_data_of_a_plant_configured_after_the_read_names_the_missing_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    PlantConfiguration.objects.filter(plant=plant).update(created_at=NOW + timedelta(hours=1))
    result = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="satellite",
    )
    assert result["source_kind"] == "estimated_actuals"
    assert result["configuration_id"] is None
    assert result["result"] is None
    assert result["unavailable_reason"] == "configuration_unavailable"


def test_eligible_archived_forecast_survives_a_later_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    original_id = plant.current_configuration_id
    publish_snapshot(snapshot_for(system, DAY))
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    archived = plant_outlook(*args, target_date=DAY, resolution_minutes=60)
    revise_plant(*args, PlantParameters(name="Revised", dc_capacity_mw=260, export_limit_mw=200))
    result = plant_outlook(*args, target_date=DAY, resolution_minutes=60)
    assert result["configuration_id"] == original_id
    expected = dict(cast(dict[str, Any], archived["result"]))
    expected["quality_flags"] = [*expected["quality_flags"], "previous_configuration"]
    assert result["result"] == expected
    assert result["freshness"] == "archived"


def test_archive_api_keeps_tenant_boundaries_after_source_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = publish_snapshot(snapshot_for(system, DAY))
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    query = {"date": DAY.isoformat(), "data_source": "forecast"}
    first = client.get(path, query)
    assert first.status_code == 200
    source.normalized_intervals[0]["power_kw"] = -1
    stored_again(source)
    assert client.get(path, query).json() == first.json()
    other = Tenant.objects.create(name="Other")
    user = User.objects.create_user("other-tenant")
    Membership.objects.create(tenant=other, user=user)
    client.force_login(user)
    assert client.get(path, query).status_code == 403
    own_path = path.replace(f"tenants/{system.tenant.pk}", f"tenants/{other.pk}")
    assert client.get(own_path, {"date": DAY.isoformat()}).status_code == 404


def actuals(system: System, *, acquired: datetime = NOW - timedelta(hours=1)) -> ForecastSnapshot:
    snapshot = snapshot_for(system, DAY, collected_at=acquired, source_kind="estimated_actuals")
    start, end = local_day_bounds(DAY, system.binding.timezone)
    snapshot.available_at = acquired
    snapshot.coverage_start, snapshot.coverage_end = start, end
    snapshot.save()
    snapshot.job.status = "succeeded"
    snapshot.job.save(update_fields=["status"])
    return snapshot


def test_past_actuals_use_declared_configuration_without_backdating_or_replacing_forecasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    reference = actuals(system)
    forecast_source = publish_snapshot(snapshot_for(system, DAY, power_kw=0.5))
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    archived = plant_outlook(*args, target_date=DAY, resolution_minutes=30)
    archived_payload = archived["result"]
    assert isinstance(archived_payload, dict)
    assert archived_payload["energy_mwh"] == 1560
    PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(created_at=NOW)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "satellite"})
    assert response.status_code == 200
    data = response.json()
    assert data["source_kind"] == "estimated_actuals"
    assert data["configuration_id"] == plant.current_configuration_id
    assert data["calibration_id"] is None
    assert data["freshness"] == "archived"
    assert data["unavailable_reason"] is None
    assert data["result"]["energy_mwh"] == 1248
    assert len(data["result"]["intervals"]) == 48
    assert "historical_configuration_assumed" in data["result"]["quality_flags"]
    assert "estimated_actuals_not_metered" in data["result"]["quality_flags"]
    assert data["result"]["provenance"]["snapshot_id"] == reference.pk
    assert data["result"]["provenance"]["selection_policy"] == HISTORY_SELECTION_POLICY
    hourly = client.get(
        path,
        {"date": DAY.isoformat(), "data_source": "satellite", "resolution_minutes": "60"},
    )
    assert hourly.status_code == 200
    assert len(hourly.json()["result"]["intervals"]) == 24
    assert hourly.json()["result"]["energy_mwh"] == 1248
    assert ForecastIssuance.objects.get(snapshot=forecast_source).payloads["30"] == {
        **archived_payload,
        "quality_flags": [
            flag for flag in archived_payload["quality_flags"] if flag != "archived_forecast"
        ],
    }
    assert PlantConfiguration.objects.get(pk=plant.current_configuration_id).created_at == NOW
    client.force_login(User.objects.create_user("unrelated-user"))
    query = {"date": DAY.isoformat(), "data_source": "satellite"}
    assert client.get(path, query).status_code == 403


@pytest.mark.parametrize("creation", ["before_day", "during_day", "after_day"])
def test_past_actuals_follow_configuration_at_day_start_after_binding_change(
    monkeypatch: pytest.MonkeyPatch,
    creation: str,
) -> None:
    system, plant = make_plant(monkeypatch)
    old_configuration_id = plant.current_configuration_id
    assert old_configuration_id is not None
    if creation != "before_day":
        start, end = local_day_bounds(DAY, system.binding.timezone)
        PlantConfiguration.objects.filter(pk=old_configuration_id).update(
            created_at=(start if creation == "during_day" else end) + timedelta(hours=1)
        )
    reference = actuals(system)
    new_binding = SiteBinding.objects.get(pk=system.binding.pk)
    new_binding.pk = None
    new_binding.site_id = "new-site"
    new_binding.region_revision += 1
    new_binding.save()
    Region.objects.filter(pk=system.region.pk).update(
        approved_binding=new_binding, revision=new_binding.region_revision
    )
    revised = revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        PlantParameters(name="Revised", dc_capacity_mw=130, export_limit_mw=100),
    )
    assert revised.current_configuration_id != old_configuration_id
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    past = plant_outlook(*args, target_date=DAY, resolution_minutes=30, source="satellite")
    assert past["configuration_id"] == old_configuration_id
    assert past["freshness"] == "archived"
    payload = past["result"]
    assert isinstance(payload, dict)
    assert payload["provenance"]["snapshot_id"] == reference.pk
    assert "estimated_actuals_not_metered" in payload["quality_flags"]
    assert ("historical_configuration_assumed" in payload["quality_flags"]) == (
        creation != "before_day"
    )
    today = plant_outlook(*args, target_date=NOW.date(), resolution_minutes=30)
    assert today["configuration_id"] == revised.current_configuration_id
    assert today["result"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"collected_at": NOW + timedelta(seconds=1)},
        {"available_at": NOW + timedelta(seconds=1)},
        {"issued_at": NOW + timedelta(seconds=1)},
        {"collected_at": datetime(2026, 11, 7, tzinfo=NOW.tzinfo)},
        {"coverage_start": NOW, "coverage_end": NOW + timedelta(minutes=30)},
        {
            "coverage_start": datetime(2026, 11, 6, tzinfo=NOW.tzinfo),
            "coverage_end": datetime(2026, 11, 7, tzinfo=NOW.tzinfo),
        },
        {"source_kind": "forecast"},
        {"available_at": None},
    ],
)
def test_history_selection_excludes_unavailable_or_unmatched_revisions(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    system, _ = make_plant(monkeypatch)
    original = actuals(system, acquired=NOW - timedelta(hours=2))
    changed = actuals(system)
    stored_again(ForecastSnapshot.objects.get(pk=changed.pk), **changes)
    selected = select_actual_snapshot(system.binding, DAY, NOW)
    assert selected is not None
    assert selected.pk == original.pk


def test_history_selection_keeps_acquisition_and_publication_cutoffs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    first = actuals(system, acquired=NOW - timedelta(hours=2))
    second = actuals(system)
    second.available_at = NOW
    second.issued_at = NOW - timedelta(hours=1)
    stored_again(second)
    assert select_actual_snapshot(system.binding, DAY, NOW) == second
    assert select_actual_snapshot(system.binding, DAY, NOW - timedelta(minutes=1)) == first
    second.job.status = "stale"
    second.job.save()
    assert select_actual_snapshot(system.binding, DAY, NOW) == first
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    data = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=30,
        source="satellite",
    )
    assert data["freshness"] == "archived"


def test_missing_satellite_data_stays_missing_beside_a_saved_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    publish_snapshot(snapshot_for(system, DAY))
    forecast = plant_outlook(*args, target_date=DAY, resolution_minutes=30, source="forecast")
    assert forecast["source_kind"] == "forecast"
    assert forecast["result"] is not None
    missing = plant_outlook(*args, target_date=DAY, resolution_minutes=30, source="satellite")
    assert missing["result"] is None
    assert missing["unavailable_reason"] == "estimated_actuals_unavailable"
    assert missing["freshness"] == "missing"
    future = plant_outlook(
        *args, target_date=NOW.date() + timedelta(days=1), resolution_minutes=30, source="forecast"
    )
    assert future["source_kind"] == "forecast"


@pytest.mark.parametrize(
    "values",
    [
        {"source": "unknown"},
        {"resolution_minutes": True},
        {"resolution_minutes": 15},
        {"target_date": NOW.date() + timedelta(days=1)},
        {"target_date": NOW.date() - timedelta(days=367)},
    ],
)
def test_history_rejects_incomplete_dates_and_invalid_arguments(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]
) -> None:
    system, plant = make_plant(monkeypatch)
    arguments: dict[str, Any] = {
        "target_date": DAY,
        "resolution_minutes": 30,
        "source": "satellite",
    } | values
    with pytest.raises(ValidationError):
        plant_outlook(system.member, system.tenant.pk, system.region.pk, plant.pk, **arguments)


@pytest.mark.parametrize("scope", ["plant", "region"])
@pytest.mark.parametrize("complete", [False, True])
def test_polar_night_satellite_history_states_whether_its_composed_input_is_complete(
    monkeypatch: pytest.MonkeyPatch, scope: str, complete: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    day = date(2026, 11, 14)
    SiteBinding.objects.filter(pk=system.binding.pk).update(latitude=80, longitude=20)
    Region.objects.filter(pk=system.region.pk).update(latitude=80, longitude=20)
    system.binding.refresh_from_db()
    start, end = local_day_bounds(day, system.binding.timezone)
    sources = []
    for first, last in ((0, 12), (12, 24)):
        source = snapshot_for(
            system,
            day,
            power_kw=0,
            source_kind="estimated_actuals",
            collected_at=start + timedelta(hours=last),
        )
        source.normalized_intervals = source.normalized_intervals[first * 2 : last * 2]
        if last == 24 and not complete:
            source.normalized_intervals[-1]["power_kw"] = None
        source.job.endpoint = "estimated_actuals"
        source.job.save(update_fields=("endpoint",))
        sources.append(publish_snapshot(source))
    monkeypatch.setattr("pyracast.plants.history.timezone.now", lambda: end + timedelta(hours=1))
    client = Client()
    client.force_login(system.member)
    prefix = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}"
    path = f"{prefix}/plants/{plant.pk}/forecast/" if scope == "plant" else f"{prefix}/forecast/"
    response = client.get(path, {"date": day.isoformat(), "data_source": "satellite"})
    assert response.status_code == 200, response.content
    payload = response.json()
    assert payload["result"]["daylight"]["status"] == "polar_night"
    assert payload["source_kind"] == "estimated_actuals"
    irradiance = payload["regional_irradiance"] if scope == "plant" else payload["result"]
    assert irradiance["provenance"]["input_day_complete"] is complete
    if complete:
        provenance = payload["result"]["provenance"]
        if scope == "plant":
            assert payload["result"]["complete"] is True
            assert payload["result"]["energy_mwh"] == 0
        assert set(provenance["source_snapshot_ids"]) == {source.pk for source in sources}


def test_corrupt_history_cannot_return_a_fabricated_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot = actuals(system)
    snapshot.normalized_intervals[0]["power_kw"] = -1
    stored_again(snapshot)
    with raises_code("calculation_out_of_range"):
        plant_outlook(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            target_date=DAY,
            resolution_minutes=30,
            source="satellite",
        )


def test_history_selection_preserves_complete_day_when_newer_rolling_window_is_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    complete = actuals(system, acquired=NOW - timedelta(hours=2))
    partial = actuals(system)
    partial.normalized_intervals = partial.normalized_intervals[6:]
    stored_again(partial)
    assert select_actual_snapshot(system.binding, DAY, NOW) == complete
    complete.normalized_intervals = complete.normalized_intervals[:-6]
    stored_again(complete)
    assert select_actual_snapshot(system.binding, DAY, NOW) == partial
