from datetime import date, timedelta

import pytest
from django.test import Client

from pyracast.accounts.models import Tenant
from tests.support.domain import NOW
from tests.support.plant_operations import make_plant, publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 8)


def test_archive_api_replays_published_source_without_creating_a_plant_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot = publish_snapshot(snapshot_for(system, DAY))
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "forecast"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["source_kind"] == "forecast"
    assert payload["result"]["provenance"]["snapshot_id"] == snapshot.pk
    assert "archived_forecast" in payload["result"]["quality_flags"]
    assert (
        client.get(
            path,
            {
                "date": (NOW.date() + timedelta(days=1)).isoformat(),
                "data_source": "satellite",
            },
        ).status_code
        == 400
    )
    other = Tenant.objects.create(name="Other")
    other_path = f"/api/tenants/{other.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    assert (
        client.get(other_path, {"date": DAY.isoformat(), "data_source": "forecast"}).status_code
        == 403
    )


def test_a_chosen_forecast_replays_an_issuance_published_after_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = snapshot_for(system, DAY)
    publish_snapshot(source, published_at=source.collected_at + timedelta(minutes=15))
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    query = {"date": DAY.isoformat(), "data_source": "forecast"}
    response = client.get(path, query)
    assert response.status_code == 200
    original = response.json()
    assert original["result"]["provenance"]["snapshot_id"] == source.pk
    assert original["result"]["provenance"]["collected_at"] == source.collected_at.isoformat()
    replay = client.get(path, {**query, "forecast_id": str(source.pk)})
    assert replay.status_code == 200
    replayed = replay.json()
    assert replayed.pop("selection")["mode"] == "manual"
    assert original.pop("selection")["mode"] == "auto"
    assert replayed == original
