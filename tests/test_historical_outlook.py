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
from pyracast.plants.forecasting import plant_forecast
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import ForecastIssuance, PlantConfiguration
from pyracast.plants.operations import revise_plant
from pyracast.regions.models import Region
from tests.support.domain import NOW, System
from tests.support.errors import raises_code
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant, publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 8)


@pytest.mark.parametrize("delay_minutes", [0, 15])
def test_forecast_replay_requires_electrical_publication_before_cutoff(
    monkeypatch: pytest.MonkeyPatch, delay_minutes: int
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot = snapshot_for(system, DAY)
    published = snapshot.collected_at + timedelta(minutes=delay_minutes)
    publish_snapshot(snapshot, published_at=published)
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    unavailable = plant_forecast(
        *args, target_date=DAY, resolution_minutes=60, as_of=published - timedelta(microseconds=1)
    )
    assert unavailable["result"] is None
    available = plant_forecast(*args, target_date=DAY, resolution_minutes=60, as_of=published)
    assert available["snapshot_id"] == snapshot.pk
    assert available["as_of"] == published.isoformat()
    replay = plant_forecast(
        *args,
        target_date=DAY,
        resolution_minutes=60,
        as_of=datetime.fromisoformat(available["as_of"]),
    )
    assert replay == available


def test_later_pre_sunrise_forecast_replaces_previous_evening_archive(
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
    before = plant_forecast(
        *args,
        target_date=DAY,
        resolution_minutes=60,
        as_of=latest.collected_at - timedelta(seconds=1),
    )
    after = plant_forecast(*args, target_date=DAY, resolution_minutes=60, as_of=latest.collected_at)
    assert before["snapshot_id"] == previous.pk
    assert after["snapshot_id"] == latest.pk
    assert after["as_of"] == latest.collected_at.isoformat()
    assert after["result"] != before["result"]


@pytest.mark.parametrize("source", ["forecast", "auto", "archived_forecast"])
def test_new_configuration_cannot_reconstruct_a_historical_forecast(
    monkeypatch: pytest.MonkeyPatch, source: str
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
        source=source,
    )
    assert result["result"] is None
    assert result["unavailable_reason"] == (
        "estimated_actuals_unavailable" if source == "auto" else "configuration_unavailable"
    )
    assert result["outage_scenario"] is None


def test_eligible_archived_forecast_survives_a_later_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    original_id = plant.current_configuration_id
    publish_snapshot(snapshot_for(system, DAY))
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    archived = plant_forecast(*args, target_date=DAY, resolution_minutes=60)
    revise_plant(*args, name="Revised", dc_capacity_mw=260, export_limit_mw=200)
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
    query = {"date": DAY.isoformat(), "data_source": "archived_forecast"}
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
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "auto"})
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
        {"date": DAY.isoformat(), "data_source": "estimated_actuals", "resolution_minutes": "60"},
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
    assert client.get(path, {"date": DAY.isoformat(), "data_source": "auto"}).status_code == 403


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
        name="Revised",
        dc_capacity_mw=130,
        export_limit_mw=100,
    )
    assert revised.current_configuration_id != old_configuration_id
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    for source in ("auto", "estimated_actuals"):
        past = plant_outlook(*args, target_date=DAY, resolution_minutes=30, source=source)
        assert past["configuration_id"] == old_configuration_id
        assert past["freshness"] == "archived"
        payload = past["result"]
        assert isinstance(payload, dict)
        assert payload["provenance"]["snapshot_id"] == reference.pk
        assert "estimated_actuals_not_metered" in payload["quality_flags"]
        assert ("historical_configuration_assumed" in payload["quality_flags"]) == (
            creation != "before_day"
        )
    replay = plant_outlook(
        *args, target_date=DAY, resolution_minutes=30, source="estimated_actuals", as_of=NOW
    )
    assert replay["configuration_id"] == revised.current_configuration_id
    assert replay["result"] is None
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
        source="estimated_actuals",
    )
    assert data["freshness"] == "archived"


def test_missing_history_falls_back_only_when_automatic(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    publish_snapshot(snapshot_for(system, DAY))
    automatic = plant_outlook(
        *args, target_date=DAY, resolution_minutes=30, source="auto", as_of=NOW
    )
    assert automatic["source_kind"] == "forecast"
    assert automatic["result"] is not None
    missing = plant_outlook(
        *args, target_date=DAY, resolution_minutes=30, source="estimated_actuals"
    )
    assert missing["result"] is None
    assert missing["unavailable_reason"] == "estimated_actuals_unavailable"
    assert missing["freshness"] == "missing"
    PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(created_at=NOW)
    missing_config = plant_outlook(
        *args,
        target_date=DAY,
        resolution_minutes=60,
        source="estimated_actuals",
        as_of=NOW - timedelta(seconds=1),
    )
    assert missing_config["configuration_id"] is None
    assert missing_config["unavailable_reason"] == "configuration_unavailable"
    future = plant_outlook(
        *args, target_date=NOW.date() + timedelta(days=1), resolution_minutes=30, source="auto"
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
        {"as_of": NOW.replace(tzinfo=None)},
        {"as_of": NOW + timedelta(seconds=1)},
    ],
)
def test_history_rejects_incomplete_dates_and_invalid_arguments(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]
) -> None:
    system, plant = make_plant(monkeypatch)
    arguments: dict[str, Any] = {
        "target_date": DAY,
        "resolution_minutes": 30,
        "source": "estimated_actuals",
    } | values
    with pytest.raises(ValidationError):
        plant_outlook(system.member, system.tenant.pk, system.region.pk, plant.pk, **arguments)


@pytest.mark.parametrize("scope", ["plant", "region"])
@pytest.mark.parametrize("complete", [False, True])
def test_polar_night_automatic_history_requires_complete_composed_actual_input(
    monkeypatch: pytest.MonkeyPatch, scope: str, complete: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    day = date(2026, 11, 14)
    SiteBinding.objects.filter(pk=system.binding.pk).update(latitude=80, longitude=20)
    Region.objects.filter(pk=system.region.pk).update(latitude=80, longitude=20)
    system.binding.refresh_from_db()
    start, end = local_day_bounds(day, system.binding.timezone)
    forecast = publish_snapshot(
        snapshot_for(system, day, power_kw=0.1, collected_at=start + timedelta(hours=3))
    )
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
    response = client.get(path, {"date": day.isoformat(), "data_source": "auto"})
    assert response.status_code == 200, response.content
    payload = response.json()
    assert payload["result"]["daylight"]["status"] == "polar_night"
    if complete:
        assert payload["source_kind"] == "estimated_actuals"
        provenance = payload["result"]["provenance"]
        irradiance = payload["regional_irradiance"] if scope == "plant" else payload["result"]
        assert irradiance["provenance"]["input_day_complete"] is True
        if scope == "plant":
            assert payload["result"]["complete"] is True
            assert payload["result"]["energy_mwh"] == 0
        assert set(provenance["source_snapshot_ids"]) == {source.pk for source in sources}
    else:
        assert payload["source_kind"] == "forecast"
        assert payload["result"]["provenance"]["source_snapshot_ids"] == [forecast.pk]


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
            source="estimated_actuals",
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
