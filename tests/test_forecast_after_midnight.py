from datetime import date, timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from pyracast.forecasts.models import ForecastResult, ForecastSnapshot, RequestAttempt
from pyracast.irradiance import local_day_bounds
from tests.support.automatic_forecast_selection import reference_transition
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant, publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 14)


@pytest.mark.parametrize("damage", ["14.11.2026 03:00", None], ids=["start-text", "null-row"])
def test_damaged_saved_satellite_data_is_refused_without_a_provider_request(
    monkeypatch: pytest.MonkeyPatch, damage: str | None
) -> None:
    system, plant = make_plant(monkeypatch)
    _, end = local_day_bounds(DAY, system.binding.timezone)
    saved = publish_snapshot(
        snapshot_for(system, DAY, source_kind="estimated_actuals", collected_at=end)
    )
    rows: list[Any] = [dict(row) for row in saved.normalized_intervals]
    rows[3] = None if damage is None else {**rows[3], "start": damage}
    stored_again(saved, normalized_intervals=rows)
    monkeypatch.setattr(timezone, "now", lambda: end + timedelta(hours=1))
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    requests = RequestAttempt.objects.count()
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "satellite"})
    assert response.status_code == 400, response.content
    assert response.json() == {
        "code": "calculation_out_of_range",
        "message": "The forecast for this day could not be calculated.",
    }
    assert RequestAttempt.objects.count() == requests


@pytest.mark.parametrize("scope", ["plant", "region"])
def test_after_midnight_a_day_keeps_only_its_forecast_saved_before_sunrise(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    system, plant = make_plant(monkeypatch)
    start, end = local_day_bounds(DAY, system.binding.timezone)
    first = publish_snapshot(
        snapshot_for(system, DAY, power_kw=0.1, collected_at=start + timedelta(hours=3))
    )
    latest = snapshot_for(system, DAY, power_kw=0.2, collected_at=start + timedelta(hours=12))
    latest.normalized_intervals = latest.normalized_intervals[24:]
    publish_snapshot(latest)
    monkeypatch.setattr(timezone, "now", lambda: end - timedelta(microseconds=1))
    client = Client()
    client.force_login(system.member)
    prefix = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}"
    path = f"{prefix}/plants/{plant.pk}/forecast/" if scope == "plant" else f"{prefix}/forecast/"

    def read(source: str = "forecast") -> dict[str, Any]:
        response = client.get(path, {"date": DAY.isoformat(), "data_source": source})
        assert response.status_code == 200, response.content
        body: dict[str, Any] = response.json()
        return body

    before = read()
    requests = RequestAttempt.objects.count()
    result_rows = list(ForecastResult.objects.values())
    monkeypatch.setattr(timezone, "now", lambda: end)
    after = read()

    assert before["result"]["provenance"]["source_snapshot_ids"] == [latest.pk, first.pk]
    assert [option["forecast_id"] for option in before["selection"]["options"]] == [
        latest.pk,
        first.pk,
    ]
    assert after["source_kind"] == "forecast"
    assert after["freshness"] == "archived"
    assert after["result"]["provenance"]["snapshot_id"] == first.pk
    assert [option["forecast_id"] for option in after["selection"]["options"]] == [first.pk]
    assert read("satellite")["result"] is None
    if scope == "plant":
        assert after["result"]["energy_mwh"] == pytest.approx(312)
    assert list(ForecastResult.objects.values()) == result_rows
    assert RequestAttempt.objects.count() == requests


@pytest.mark.parametrize("scope", ["plant", "region"])
def test_a_day_without_forecast_or_satellite_data_stays_unavailable(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    system, plant = make_plant(monkeypatch)
    _, end = local_day_bounds(DAY, system.binding.timezone)
    monkeypatch.setattr(timezone, "now", lambda: end)
    client = Client()
    client.force_login(system.member)
    prefix = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}"
    path = f"{prefix}/plants/{plant.pk}/forecast/" if scope == "plant" else f"{prefix}/forecast/"
    forecast = client.get(path, {"date": DAY.isoformat(), "data_source": "forecast"}).json()
    satellite = {"date": DAY.isoformat(), "data_source": "satellite"}
    missing = client.get(path, satellite).json()
    assert (forecast["source_kind"], forecast["result"]) == ("forecast", None)
    assert forecast["unavailable_reason"] == "forecast_unavailable"
    assert (missing["source_kind"], missing["result"]) == ("estimated_actuals", None)
    assert missing["unavailable_reason"] == "estimated_actuals_unavailable"
    assert not ForecastSnapshot.objects.exists()
    actual = snapshot_for(system, DAY, source_kind="estimated_actuals", collected_at=end)
    actual.normalized_intervals = actual.normalized_intervals[24:25]
    publish_snapshot(actual)
    partial = client.get(path, satellite).json()
    assert partial["source_kind"] == "estimated_actuals"
    assert partial["result"]["complete"] is False


def test_a_reference_transition_day_keeps_its_original_forecast_after_local_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, original_id = reference_transition(monkeypatch)
    day = date(2026, 11, 11)
    _, end = local_day_bounds(day, system.binding.timezone)
    monkeypatch.setattr(timezone, "now", lambda: end - timedelta(microseconds=1))
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    query = {"date": day.isoformat(), "data_source": "forecast"}
    before = client.get(path, query).json()
    monkeypatch.setattr(timezone, "now", lambda: end)
    response = client.get(path, query)
    assert response.status_code == 200, response.content
    after = response.json()
    assert after["configuration_id"] == before["configuration_id"]
    assert after["result"]["energy_mwh"] == before["result"]["energy_mwh"] == pytest.approx(312)
    assert after["result"]["provenance"]["snapshot_id"] == original_id
    assert "reference_transition_fallback" in before["result"]["quality_flags"]
    assert after["selection"]["forecast_id"] == original_id
