from datetime import date, timedelta

import pytest
from django.test import Client

from pyracast.accounts.models import Tenant
from pyracast.plants.models import PlantForecast
from tests.test_domain import NOW
from tests.test_plant_operations import make_plant, publish_snapshot, snapshot_for

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
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "archived_forecast"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["source_kind"] == "forecast"
    assert payload["result"]["provenance"]["snapshot_id"] == snapshot.pk
    assert "archived_forecast" in payload["result"]["quality_flags"]
    assert not PlantForecast.objects.exists()
    assert (
        client.get(
            path,
            {
                "date": (NOW.date() + timedelta(days=1)).isoformat(),
                "data_source": "archived_forecast",
            },
        ).status_code
        == 400
    )
    other = Tenant.objects.create(name="Other")
    other_path = f"/api/tenants/{other.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    assert (
        client.get(
            other_path, {"date": DAY.isoformat(), "data_source": "archived_forecast"}
        ).status_code
        == 403
    )
