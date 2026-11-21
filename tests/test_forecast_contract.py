import json
from datetime import date

import pytest
from django.test import Client

from tests.support.forecast_history import (
    PLANT_READS,
    REGION_READS,
    TODAY,
    UNCOVERED,
    add_history,
    usable_calibration,
)
from tests.support.openapi import components, validate
from tests.support.performance_history import FIRST, LAST, performance_history
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)

HISTORY = "native-then-authorized-scale-complete-actuals-v4"
COMPOSED = "native-estimated-actuals-coverage-v1"
ARCHIVE = "latest-published-pre-daylight-issuance-v1"
TYPED_BLOBS = {
    "ForecastOutput": {"result": "IrradianceResult"},
    "PlantForecastOutput": {
        "result": "PlantForecastResult",
        "regional_irradiance": "IrradianceSummary",
        "outage_scenario": "OutageScenario",
        "production_diagnostic": "ProductionDiagnostic",
    },
}


def _performance(client: Client, url: str, comparison: str) -> dict[str, object]:
    response = client.get(
        url, {"start_date": str(FIRST), "end_date": str(LAST), "comparison": comparison}
    )
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    assert validate("PerformanceOutput", body) == []
    return body


def test_performance_states_each_days_selection_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    performance_history(system, plant)
    client = Client()
    client.force_login(system.member)
    url = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/performance/"
    )
    historical = _performance(client, url, "historical_model")
    assert "evaluation_kind" not in historical
    days = historical["days"]
    assert isinstance(days, list)
    assert {day["date"]: day["selection_policy"] for day in days} == {
        "2026-11-03": HISTORY,
        "2026-11-04": COMPOSED,
        "2026-11-05": HISTORY,
        "2026-11-06": None,
        "2026-11-07": COMPOSED,
        "2026-11-08": HISTORY,
    }
    archived = _performance(client, url, "archived_forecast")["days"]
    assert isinstance(archived, list)
    assert {day["selection_policy"] for day in archived} == {ARCHIVE}


def test_performance_schema_types_reasons_and_policies_as_choices() -> None:
    schemas = components()
    day = schemas["PerformanceDay"]["properties"]
    assert "evaluation_kind" not in schemas["PerformanceOutput"]["properties"]
    reasons = schemas[day["reason"]["oneOf"][0]["$ref"].rsplit("/", 1)[1]]["enum"]
    assert set(reasons) == {
        "configuration_not_comparable",
        "forecast_not_available",
        "estimated_actuals_not_available",
        "invalid_forecast_data",
        "incomplete_or_invalid_day",
    }
    policies = schemas[day["selection_policy"]["oneOf"][0]["$ref"].rsplit("/", 1)[1]]["enum"]
    assert set(policies) == {HISTORY, COMPOSED, ARCHIVE}
    assert day["calibration_in_sample"]["nullable"] is True


def test_forecast_schema_declares_freshness_and_typed_payloads() -> None:
    schemas = components()
    for component, blobs in TYPED_BLOBS.items():
        properties = schemas[component]["properties"]
        freshness = schemas[properties["freshness"]["$ref"].rsplit("/", 1)[1]]["enum"]
        assert set(freshness) == {"fresh", "stale", "missing", "archived"}
        stale = properties["stale_reason"]["oneOf"][0]["$ref"].rsplit("/", 1)[1]
        assert set(schemas[stale]["enum"]) == {"collection_overdue", "connection_inactive"}
        for field, typed in blobs.items():
            assert f"#/components/schemas/{typed}" in json.dumps(properties[field]), field


@pytest.mark.parametrize(
    ("scope", "component", "reads"),
    [
        ("plant", "PlantForecastOutput", PLANT_READS),
        ("region", "ForecastOutput", REGION_READS),
    ],
)
def test_every_forecast_branch_matches_its_schema(
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    component: str,
    reads: tuple[tuple[str, date], ...],
) -> None:
    system, plant = make_plant(monkeypatch)
    usable_calibration(system, plant)
    add_history(system, plant, range(2))
    client = Client()
    client.force_login(system.member)
    region = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
    url = f"{region}plants/{plant.pk}/forecast/" if scope == "plant" else f"{region}forecast/"
    errors = {}
    for source, day in (*reads, ("auto", UNCOVERED), ("forecast", TODAY)):
        response = client.get(url, {"date": str(day), "data_source": source})
        assert response.status_code == 200, (source, day)
        errors[source, day] = validate(component, response.json())
    assert errors == {read: [] for read in errors}
