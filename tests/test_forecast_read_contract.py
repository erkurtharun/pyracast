import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import Plant
from pyracast.plants.operations import create_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.models import Region
from pyracast.regions.operations import submit_region
from pyracast.weather_data import serialize_weather
from tests.support.domain import NOW, System
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant, publish_snapshot, snapshot_for
from tests.support.weather_collection import WeatherFixture

pytestmark = pytest.mark.django_db(transaction=True)
OPENAPI = Path(__file__).resolve().parents[1] / "web" / "openapi.json"
DAY = date(2026, 11, 10)


def plant_path(system: System, plant: Plant, endpoint: str = "forecast") -> str:
    return (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/{endpoint}/"
    )


def region_path(system: System, region: Region | None = None) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{(region or system.region).pk}/forecast/"


def client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def viewer_of(system: System) -> User:
    viewer = User.objects.create(username="viewer")
    Membership.objects.create(user=viewer, tenant=system.tenant, role=WorkspaceRole.VIEWER)
    return viewer


def test_viewer_reads_the_same_forecast_bodies_as_the_workspace_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, DAY, collected_at=NOW))
    admin, viewer = client_for(system.member), client_for(viewer_of(system))
    past = (DAY - timedelta(days=3)).isoformat()
    reads: list[tuple[str, dict[str, Any]]] = [
        (plant_path(system, plant), {"date": DAY.isoformat()}),
        (region_path(system), {"date": DAY.isoformat()}),
        (plant_path(system, plant, "performance"), {"start_date": past, "end_date": past}),
    ]
    for path, params in reads:
        expected = admin.get(path, params)
        assert expected.status_code == 200
        response = viewer.get(path, params)
        assert response.status_code == 200
        assert response.json() == expected.json()
    assert admin.get(plant_path(system, plant), {"date": DAY.isoformat()}).json()["result"]
    assert admin.get(region_path(system), {"date": DAY.isoformat()}).json()["result"]


def test_forecast_reads_answer_not_found_outside_the_url_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    sibling_region = submit_region(
        system.member,
        system.tenant.pk,
        name="Sibling",
        latitude=41,
        longitude=30,
        timezone="Europe/Istanbul",
    )
    sibling = create_plant(
        system.member,
        system.tenant.pk,
        sibling_region.pk,
        PlantParameters(name="Sibling", dc_capacity_mw=10),
    )
    other_member = User.objects.create(username="other-member")
    other_tenant = Tenant.objects.create(name="Other")
    Membership.objects.create(user=other_member, tenant=other_tenant, role=WorkspaceRole.ADMIN)
    foreign_region = submit_region(
        other_member,
        other_tenant.pk,
        name="Foreign",
        latitude=42,
        longitude=31,
        timezone="Europe/Istanbul",
    )
    client = client_for(system.member)
    params = {"date": DAY.isoformat()}
    foreign_plant = client.get(plant_path(system, sibling), params)
    assert foreign_plant.status_code == 404
    assert foreign_plant.json()["code"] == "not_found"
    foreign = client.get(region_path(system, foreign_region), params)
    assert foreign.status_code == 404
    assert foreign.json()["code"] == "not_found"
    foreign_plants = plant_path(system, plant).replace(
        f"regions/{system.region.pk}/", f"regions/{foreign_region.pk}/"
    )
    assert client.get(foreign_plants, params).status_code == 404


@pytest.mark.parametrize("scope", ["plant", "region"])
@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"date": DAY.isoformat(), "unknown": "1"}, None),
        ({"date": ""}, "date"),
    ],
)
def test_forecast_reads_reject_unknown_keys_and_empty_dates_by_code(
    monkeypatch: pytest.MonkeyPatch, scope: str, params: dict[str, str], field: str | None
) -> None:
    system, plant = make_plant(monkeypatch)
    path = plant_path(system, plant) if scope == "plant" else region_path(system)
    response = client_for(system.member).get(path, params)
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_request"
    assert list(body.get("fields", {})) == ([field] if field else [])


def test_past_day_plant_forecast_is_the_archived_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, DAY))
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=2))
    client = client_for(system.member)
    forecast = client.get(
        plant_path(system, plant), {"date": DAY.isoformat(), "data_source": "forecast"}
    )
    archived = client.get(
        plant_path(system, plant), {"date": DAY.isoformat(), "data_source": "archived_forecast"}
    )
    assert forecast.status_code == archived.status_code == 200
    assert forecast.json()["result"] is not None
    assert forecast.json()["result"] == archived.json()["result"]


def test_region_reads_explicit_estimated_actuals_for_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=12, minutes=15)
    forecast = publish_snapshot(snapshot_for(system, DAY))
    forecast.weather_payload = serialize_weather(
        WeatherFixture().fetch(40, 29, start, start + timedelta(days=1))
    )
    stored_again(forecast)
    actual = snapshot_for(
        system,
        DAY,
        collected_at=now - timedelta(minutes=1),
        source_kind="estimated_actuals",
    )
    actual.job.endpoint = "estimated_actuals"
    actual.job.save(update_fields=("endpoint",))
    publish_snapshot(actual)
    monkeypatch.setattr(timezone, "now", lambda: now)
    response = client_for(system.member).get(
        region_path(system), {"date": DAY.isoformat(), "data_source": "estimated_actuals"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source_kind"] == "estimated_actuals"
    assert body["unavailable_reason"] is None
    assert body["result"]["provenance"]["source_kind"] == "estimated_actuals"
    assert body["result"]["provenance"]["snapshot_id"] == actual.pk
    weather = body["result"]["weather_reference"]
    assert weather["energy_kwh_m2"] == pytest.approx(14.4)
    assert weather["intervals"][0]["ghi_wm2"] == 600


ENVELOPE = {
    "date",
    "timezone",
    "source_kind",
    "freshness",
    "stale_reason",
    "unavailable_reason",
    "result",
}
REGION_ENVELOPE = ENVELOPE | {"collected_at", "issued_at", "binding_id"}
PLANT_ENVELOPE = ENVELOPE | {
    "plant_id",
    "as_of",
    "configuration_id",
    "calibration_id",
    "regional_irradiance",
    "outage_scenario",
    "production_diagnostic",
}


def test_each_forecast_read_sends_exactly_its_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot = publish_snapshot(snapshot_for(system, DAY, collected_at=NOW))
    client = client_for(system.member)
    today = {"date": DAY.isoformat()}
    region = client.get(region_path(system), today).json()
    issued = client.get(plant_path(system, plant), {**today, "data_source": "forecast"}).json()
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=2))
    client = client_for(system.member)
    actuals = {**today, "data_source": "estimated_actuals"}
    archived = client.get(plant_path(system, plant), {**today, "data_source": "forecast"}).json()
    plant_actuals = client.get(plant_path(system, plant), actuals).json()
    region_actuals = client.get(region_path(system), actuals).json()

    assert set(region) == set(region_actuals) == REGION_ENVELOPE
    assert region["binding_id"] == system.binding.pk
    declared = PLANT_ENVELOPE | {"issuance_id", "snapshot_id"}
    assert set(issued) == set(archived) == declared
    assert PLANT_ENVELOPE <= set(plant_actuals) <= declared
    assert archived["snapshot_id"] == snapshot.pk


def documented(name: str) -> tuple[set[str], set[str]]:
    component = json.loads(OPENAPI.read_text(encoding="utf-8"))["components"]["schemas"][name]
    return set(component.get("required", ())), set(component["properties"])


def test_the_performance_read_sends_exactly_its_documented_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, DAY, collected_at=NOW))
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=2))
    client = client_for(system.member)
    period = {"start_date": DAY.isoformat(), "end_date": DAY.isoformat()}
    required, declared = documented("PerformanceOutput")
    day_required, day_declared = documented("PerformanceDay")
    for comparison in ("archived_forecast", "historical_model"):
        response = client.get(
            plant_path(system, plant, "performance"), {**period, "comparison": comparison}
        )
        assert response.status_code == 200
        body = response.json()
        assert set(body) == required == declared
        assert body["timezone"] == system.binding.timezone
        assert body["plant_id"] == plant.pk
        assert [day["date"] for day in body["days"]] == [DAY.isoformat()]
        assert day_required <= set(body["days"][0]) <= day_declared


def test_forecast_envelopes_share_one_documented_vocabulary() -> None:
    schema = json.loads(OPENAPI.read_text(encoding="utf-8"))
    components = schema["components"]["schemas"]
    region, plant = components["ForecastOutput"], components["PlantForecastOutput"]
    performance = components["PerformanceOutput"]
    assert set(region["properties"]) == REGION_ENVELOPE
    assert set(plant["properties"]) == PLANT_ENVELOPE | {"issuance_id", "snapshot_id"}
    for field in ENVELOPE - {"result"}:
        assert region["properties"][field] == plant["properties"][field]
    assert set(performance["properties"]) & set(plant["properties"]) == {
        "timezone",
        "plant_id",
        "configuration_id",
    }
    for field in ("timezone", "plant_id", "configuration_id"):
        assert performance["properties"][field] == plant["properties"][field]
    assert components[region["properties"]["freshness"]["$ref"].rsplit("/", 1)[1]]["enum"] == [
        "fresh",
        "stale",
        "missing",
        "archived",
    ]
    for name in ("ForecastOutput", "PlantForecastOutput", "PerformanceOutput"):
        operations = [
            operation
            for methods in schema["paths"].values()
            for operation in methods.values()
            if operation["responses"]
            .get("200", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema")
            == {"$ref": f"#/components/schemas/{name}"}
        ]
        assert len(operations) == 1
        assert {"400", "403", "404", "429"} <= set(operations[0]["responses"])
