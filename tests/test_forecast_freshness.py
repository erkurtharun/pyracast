from datetime import timedelta
from typing import Any

import pytest
from django.test import Client

from pyracast.connections.models import ProviderAccount
from tests.support.domain import NOW, System
from tests.support.forecast_history import DAY, TODAY, add_history
from tests.support.plant_operations import make_plant, publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def _get(system: System, scope: str, plant_id: int, source: str, day: Any) -> dict[str, Any]:
    client = Client()
    client.force_login(system.member)
    region = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
    url = f"{region}plants/{plant_id}/forecast/" if scope == "plant" else f"{region}forecast/"
    response = client.get(url, {"date": str(day), "data_source": source})
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.parametrize(
    ("scope", "source", "day"),
    [
        ("plant", "archived_forecast", DAY),
        ("plant", "archived_forecast", TODAY),
        ("plant", "estimated_actuals", DAY),
        ("plant", "auto", DAY),
        ("region", "estimated_actuals", DAY),
        ("region", "auto", DAY),
    ],
)
def test_a_frozen_or_completed_day_reports_archived(
    monkeypatch: pytest.MonkeyPatch, scope: str, source: str, day: Any
) -> None:
    system, plant = make_plant(monkeypatch)
    add_history(system, plant, range(1))
    body = _get(system, scope, plant.pk, source, day)
    assert body["result"] is not None
    assert (body["freshness"], body["stale_reason"]) == ("archived", None)


@pytest.mark.parametrize("scope", ["plant", "region"])
def test_a_live_forecast_from_an_overdue_collection_is_stale(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(
        snapshot_for(system, TODAY + timedelta(days=1), collected_at=NOW - timedelta(hours=25))
    )
    body = _get(system, scope, plant.pk, "forecast", TODAY + timedelta(days=1))
    assert body["result"] is not None
    assert (body["freshness"], body["stale_reason"]) == ("stale", "collection_overdue")


@pytest.mark.parametrize("scope", ["plant", "region"])
def test_a_live_forecast_from_a_recent_collection_is_fresh(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    system, plant = make_plant(monkeypatch)
    add_history(system, plant, range(1))
    body = _get(system, scope, plant.pk, "forecast", TODAY + timedelta(days=1))
    assert (body["freshness"], body["stale_reason"]) == ("fresh", None)


def test_a_region_forecast_behind_an_inactive_connection_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    add_history(system, plant, range(1))
    ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=False)
    body = _get(system, "region", plant.pk, "forecast", TODAY + timedelta(days=1))
    assert body["result"] is not None
    assert (body["freshness"], body["stale_reason"]) == ("stale", "connection_inactive")


def test_a_missing_result_reports_no_stale_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    body = _get(system, "region", plant.pk, "forecast", TODAY)
    assert (body["freshness"], body["stale_reason"]) == ("missing", None)
