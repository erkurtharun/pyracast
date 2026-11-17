from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.test import Client

from pyracast.plants import production
from pyracast.plants.operations import ObservationInput
from tests.support.plant_operations import make_plant, saved_actuals
from tests.support.production_history import upload

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 8)
RISE = datetime(2026, 11, 8, 7, 17, tzinfo=UTC)
SET = datetime(2026, 11, 8, 16, 42, tzinfo=UTC)


def test_daylight_journal_preserves_boundary_slots_and_defaults_to_one_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    for offset in range(3):
        saved_actuals(system, DAY - timedelta(days=offset))
    monkeypatch.setattr(
        production,
        "daylight_window",
        lambda *args: SimpleNamespace(
            windows=((RISE, SET),), sunrise=RISE, sunset=SET, status="normal"
        ),
        raising=False,
    )
    result = production.production_history(plant, daylight_only=True)
    assert result["start_date"] == result["end_date"] == DAY
    assert result["slot_scope"] == "daylight"
    assert len(result["intervals"]) == 10
    assert result["intervals"][0]["starts_at"] == RISE.replace(minute=0)
    assert result["intervals"][-1]["ends_at"] == SET.replace(hour=17, minute=0)
    assert result["days"][0]["available_count"] == 10
    assert result["days"][0]["sunrise"] == RISE
    assert result["days"][0]["sunset"] == SET
    half = production.production_history(plant, daylight_only=True, resolution_minutes=30)
    assert len(half["intervals"]) == 20
    assert all(row["can_enter"] for row in half["intervals"])


def test_daylight_journal_keeps_night_records_without_inventing_missing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    saved_actuals(system, DAY)
    night = RISE.replace(hour=0, minute=0)
    upload(system, plant, [ObservationInput(night, night + timedelta(hours=1), 0.25)])
    monkeypatch.setattr(
        production,
        "daylight_window",
        lambda *args: SimpleNamespace(
            windows=((RISE, SET),), sunrise=RISE, sunset=SET, status="normal"
        ),
        raising=False,
    )
    result = production.production_history(plant, daylight_only=True)
    assert len(result["observations"]) == 1
    assert result["observations"][0]["energy_mwh"] == 0.25
    assert all(row["observation"] is None for row in result["intervals"])
    assert result["days"][0]["recorded_count"] == 0
    assert result["days"][0]["missing_count"] == 10


def test_daylight_journal_api_and_polar_night(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    saved_actuals(system, DAY)
    monkeypatch.setattr(
        production,
        "daylight_window",
        lambda *args: SimpleNamespace(windows=(), sunrise=None, sunset=None, status="polar_night"),
        raising=False,
    )
    client = Client()
    client.force_login(system.member)
    url = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/production/"
    )
    response = client.get(url, {"daylight_only": "true"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["slot_scope"] == "daylight"
    assert payload["intervals"] == []
    assert payload["days"][0]["daylight_status"] == "polar_night"
    assert payload["days"][0]["available_count"] == 0
    assert client.get(url, {"daylight_only": "invalid"}).status_code == 400
