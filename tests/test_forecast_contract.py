import json

import pytest
from django.test import Client

from tests.support.forecast_history import READS, add_history, usable_calibration
from tests.support.openapi import components, validate
from tests.support.performance_history import FIRST, LAST, performance_history
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)

TYPED_BLOBS = {
    "ForecastOutput": {"result": "IrradianceResult"},
    "PlantForecastOutput": {
        "result": "PlantForecastResult",
        "regional_irradiance": "IrradianceSummary",
        "outage_scenario": "OutageScenario",
        "production_diagnostic": "ProductionDiagnostic",
    },
}


@pytest.mark.parametrize("data_source", ["forecast", "satellite"])
def test_every_comparison_matches_its_schema(
    monkeypatch: pytest.MonkeyPatch, data_source: str
) -> None:
    system, plant = make_plant(monkeypatch)
    performance_history(system, plant)
    client = Client()
    client.force_login(system.member)
    url = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/performance/"
    )
    response = client.get(
        url, {"start_date": str(FIRST), "end_date": str(LAST), "data_source": data_source}
    )
    assert response.status_code == 200
    assert validate("PerformanceOutput", response.json()) == []


def test_the_comparison_schema_types_reasons_and_sources_as_choices() -> None:
    schemas = components()
    output = schemas["PerformanceOutput"]["properties"]
    day = schemas["PerformanceDay"]["properties"]
    reasons = schemas[day["reason"]["oneOf"][0]["$ref"].rsplit("/", 1)[1]]["enum"]
    assert set(reasons) == {
        "forecast_not_available",
        "estimated_actuals_not_available",
        "invalid_forecast_data",
        "incomplete_or_invalid_day",
    }
    sources = schemas[output["data_source"]["$ref"].rsplit("/", 1)[1]]["enum"]
    assert sources == ["forecast", "satellite"]


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
    ("scope", "component"), [("plant", "PlantForecastOutput"), ("region", "ForecastOutput")]
)
def test_every_forecast_branch_matches_its_schema(
    monkeypatch: pytest.MonkeyPatch, scope: str, component: str
) -> None:
    system, plant = make_plant(monkeypatch)
    usable_calibration(system, plant)
    add_history(system, plant, range(2))
    client = Client()
    client.force_login(system.member)
    region = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
    url = f"{region}plants/{plant.pk}/forecast/" if scope == "plant" else f"{region}forecast/"
    errors = {}
    for source, day in READS:
        response = client.get(url, {"date": str(day), "data_source": source})
        assert response.status_code == 200, (source, day)
        errors[source, day] = validate(component, response.json())
    assert errors == {read: [] for read in errors}
