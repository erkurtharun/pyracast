from datetime import date, timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from pyracast.accounts.models import Membership, Tenant
from pyracast.forecasts.outlook import SOURCES, Source
from pyracast.plants.models import Plant
from pyracast.plants.performance import production_comparison
from pyracast.regions.models import Region
from tests.support.domain import NOW, System
from tests.support.performance_history import FIRST, LAST, performance_history
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)


def _path(system: System, plant: Plant, endpoint: str = "performance") -> str:
    return (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/{endpoint}/"
    )


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _get(client: Client, path: str, **query: str) -> dict[str, Any]:
    response = client.get(path, query)
    assert response.status_code == 200, response.json()
    body: dict[str, Any] = response.json()
    return body


def _figure(shown: dict[str, Any]) -> float | None:
    daylight = (shown["result"] or {}).get("daylight") or {}
    return daylight.get("energy_mwh") if daylight.get("complete") else None


@pytest.mark.parametrize("data_source", SOURCES)
def test_each_compared_day_shows_the_figure_of_the_forecast_read(
    monkeypatch: pytest.MonkeyPatch, data_source: str
) -> None:
    system, plant = make_plant(monkeypatch)
    _, calibrations = performance_history(system, plant)
    client = _client(system.member)

    compared = _get(
        client,
        _path(system, plant),
        start_date=FIRST.isoformat(),
        end_date=LAST.isoformat(),
        data_source=data_source,
    )

    matched = [day for day in compared["days"] if day["status"] == "matched"]
    assert compared["data_source"] == data_source
    assert len(matched) == {"forecast": 5, "satellite": 4}[data_source]
    for day in matched:
        shown = _get(
            client,
            _path(system, plant, "forecast"),
            date=day["date"],
            data_source=data_source,
            resolution_minutes="60",
        )
        assert day["predicted_energy_mwh"] == _figure(shown)
        assert day["calibration_id"] == shown["calibration_id"]
        assert day["error_mwh"] == pytest.approx(
            day["predicted_energy_mwh"] - day["measured_energy_mwh"]
        )
        assert day["provisional_production"] is False
        if data_source == "forecast":
            assert day["forecast_id"] == shown["selection"]["forecast_id"]
    if data_source == "satellite":
        assert [calibrations[day["calibration_id"]] for day in matched] == [
            "early",
            "early",
            "late",
            "late",
        ]


def test_days_that_cannot_be_compared_name_their_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    performance_history(system, plant)

    reasons = {
        data_source: {
            day["date"]: day["reason"]
            for day in production_comparison(
                plant, start_date=FIRST, end_date=LAST, data_source=data_source
            )["days"]
            if day["status"] == "missing"
        }
        for data_source in SOURCES
    }

    assert reasons == {
        "forecast": {date(2026, 11, 7): "incomplete_or_invalid_day"},
        "satellite": {
            date(2026, 11, 6): "estimated_actuals_not_available",
            date(2026, 11, 7): "incomplete_or_invalid_day",
        },
    }


def test_accuracy_summarises_the_compared_days(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    performance_history(system, plant)

    result = production_comparison(plant, start_date=FIRST, end_date=LAST)

    errors = [day["error_mwh"] for day in result["days"] if day["status"] == "matched"]
    measured = sum(
        day["measured_energy_mwh"] for day in result["days"] if day["status"] == "matched"
    )
    assert result["matched_days"] == len(errors) == 5
    assert result["daily_mae_mwh"] == pytest.approx(sum(map(abs, errors)) / 5)
    assert result["daily_bias_mwh"] == pytest.approx(sum(errors) / 5)
    assert result["relative_error"] == pytest.approx(sum(map(abs, errors)) / measured)
    without = date(2026, 11, 6)
    empty = production_comparison(
        plant, start_date=without, end_date=without, data_source="satellite"
    )
    assert (empty["matched_days"], empty["relative_error"], empty["daily_mae_mwh"]) == (
        0,
        None,
        None,
    )


@pytest.mark.parametrize("data_source", SOURCES)
def test_the_query_count_does_not_grow_with_the_range(
    monkeypatch: pytest.MonkeyPatch, data_source: Source
) -> None:
    system, plant = make_plant(monkeypatch)
    performance_history(system, plant)
    counts = []
    for first in (FIRST, LAST - timedelta(days=89)):
        with CaptureQueriesContext(connection) as queries:
            production_comparison(plant, start_date=first, end_date=LAST, data_source=data_source)
        counts.append(len(queries))

    assert counts[0] == counts[1]


def test_the_comparison_keeps_its_access_rules_and_date_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client, path = Client(), _path(system, plant)
    day = (NOW.date() - timedelta(days=1)).isoformat()

    assert client.get(path).status_code == 401
    outsider = User.objects.create_user("outsider")
    Membership.objects.create(user=outsider, tenant=Tenant.objects.create(name="Other"))
    client.force_login(outsider)
    assert client.get(path).status_code == 403
    client.force_login(system.member)
    assert client.get(path).json()["requested_days"] == 30
    for query in (
        {"start_date": "bad"},
        {"data_source": "estimated_actuals"},
        {"comparison": "archived_forecast"},
        {"issuance_cutoff": "06:00"},
        {"daylight_only": "true"},
        {"start_date": day},
    ):
        response = client.get(path, query)
        assert (response.status_code, response.json()["code"]) == (400, "invalid_request")
    Region.objects.filter(pk=plant.region_id).update(deleted_at=NOW)
    assert client.get(path).status_code == 404
    for first, last in (
        (FIRST, FIRST - timedelta(days=1)),
        (FIRST, FIRST + timedelta(days=90)),
        (NOW.date(), NOW.date()),
        (date(2020, 1, 1), date(2020, 1, 2)),
    ):
        with pytest.raises(ValidationError):
            production_comparison(plant, start_date=first, end_date=last)
