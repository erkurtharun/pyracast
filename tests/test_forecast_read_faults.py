import logging
from datetime import date, timedelta

import pytest
from django.test import Client

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import ForecastIssuance
from pyracast.plants.performance import forecast_performance
from tests.support.domain import System
from tests.support.forecast_history import DAY
from tests.support.forecast_performance import publish
from tests.support.invariants import stored_again
from tests.support.performance_history import estimated_actuals
from tests.support.plant_operations import make_plant, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)

FAULTS = "pyracast.forecasts.faults"
DAMAGE = "08.11.2026 00:00"
MISSING = object()
DAMAGES = pytest.mark.parametrize(
    "damage", [DAMAGE, MISSING, None, 7], ids=["text", "missing", "null", "number"]
)


def _damaged_actuals(system: System, day: date, damage: object = DAMAGE) -> ForecastSnapshot:
    _, end = local_day_bounds(day, system.binding.timezone)
    snapshot = estimated_actuals(system, day, 0.3, end + timedelta(hours=1))
    rows = [dict(row) for row in snapshot.normalized_intervals]
    if damage is MISSING:
        del rows[3]["start"]
    else:
        rows[3]["start"] = damage
    return stored_again(snapshot, normalized_intervals=rows)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == FAULTS]


def _client(system: System) -> Client:
    client = Client()
    client.force_login(system.member)
    return client


@pytest.mark.parametrize("scope", ["plant", "region"])
@pytest.mark.parametrize("source", ["estimated_actuals", "auto"])
@DAMAGES
def test_damaged_estimated_actuals_fail_the_past_read_cleanly_with_one_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    scope: str,
    source: str,
    damage: object,
) -> None:
    system, plant = make_plant(monkeypatch)
    _damaged_actuals(system, DAY, damage)
    region = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
    url = f"{region}plants/{plant.pk}/forecast/" if scope == "plant" else f"{region}forecast/"
    with caplog.at_level(logging.WARNING, logger=FAULTS):
        response = _client(system).get(url, {"date": str(DAY), "data_source": source})
    assert response.status_code == 400
    assert response.json()["code"] == "calculation_out_of_range"
    [record] = _warnings(caplog)
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "ValueError" in message
    assert DAY.isoformat() in message
    assert f"{scope} {plant.pk if scope == 'plant' else system.region.pk}" in message
    assert DAMAGE not in message


def test_a_damaged_archived_issuance_fails_cleanly_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    publish(snapshot_for(system, DAY, collected_at=start - timedelta(hours=3)))
    issuance = ForecastIssuance.objects.get(target_date=DAY)
    stored_again(issuance, payloads={"60": issuance.payloads["60"]})
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    with caplog.at_level(logging.WARNING, logger=FAULTS):
        response = _client(system).get(url, {"date": str(DAY), "data_source": "archived_forecast"})
    assert response.status_code == 400
    assert response.json()["code"] == "calculation_out_of_range"
    [record] = _warnings(caplog)
    assert "KeyError" in record.getMessage()


@DAMAGES
def test_one_damaged_day_leaves_the_other_performance_days_scored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, damage: object
) -> None:
    from tests.support.performance_history import FIRST, performance_history

    system, plant = make_plant(monkeypatch)
    performance_history(system, plant)
    healthy = forecast_performance(
        plant, start_date=FIRST, end_date=DAY, comparison="historical_model"
    )
    damaged_day = date(2026, 11, 6)
    _damaged_actuals(system, damaged_day, damage)
    with caplog.at_level(logging.WARNING, logger=FAULTS):
        result = forecast_performance(
            plant, start_date=FIRST, end_date=DAY, comparison="historical_model"
        )
    days = {str(day["date"]): day for day in result["days"]}
    assert days[damaged_day.isoformat()]["reason"] == "invalid_forecast_data"
    assert result["matched_days"] == healthy["matched_days"]
    assert result["metrics"] == healthy["metrics"]
    [record] = _warnings(caplog)
    assert damaged_day.isoformat() in record.getMessage()
