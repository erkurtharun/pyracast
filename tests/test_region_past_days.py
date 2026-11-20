from datetime import timedelta
from typing import Any

import pytest
from django.test import Client

from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_archive import forecast_cutoff
from tests.support.domain import System
from tests.support.forecast_history import DAY, TODAY, add_history
from tests.support.plant_operations import make_plant, publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)

PINNED_ACTUALS = {
    "source_kind": "estimated_actuals",
    "energy_kwh_m2": 7.200000000000006,
    "partial_energy_kwh_m2": 7.200000000000006,
    "energy_mj_m2": 25.920000000000023,
    "complete": True,
    "coverage_fraction": 1.0,
    "quality_flags": ["sunlight_unknown", "unvalidated"],
    "ghi_wm2": [300.0] * 48,
}


def _history(system: System) -> Client:
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    publish_snapshot(
        snapshot_for(system, DAY, power_kw=0.6, collected_at=start + timedelta(hours=9))
    )
    client = Client()
    client.force_login(system.member)
    return client


def _read(client: Client, system: System, source: str, day: Any = DAY) -> Any:
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    return client.get(url, {"date": str(day), "data_source": source})


def _numbers(body: dict[str, Any]) -> dict[str, Any]:
    result = body["result"]
    return {
        "source_kind": body["source_kind"],
        **{
            key: result[key]
            for key in (
                "energy_kwh_m2",
                "partial_energy_kwh_m2",
                "energy_mj_m2",
                "complete",
                "coverage_fraction",
                "quality_flags",
            )
        },
        "ghi_wm2": [row["ghi_wm2"] for row in result["intervals"]],
    }


@pytest.mark.parametrize("source", ["auto", "estimated_actuals"])
def test_past_region_day_numbers_equal_the_pinned_numbers(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    system, plant = make_plant(monkeypatch)
    add_history(system, plant, range(2))
    response = _read(_history(system), system, source)
    assert response.status_code == 200
    assert _numbers(response.json()) == PINNED_ACTUALS


def test_region_archived_forecast_is_the_forecast_published_before_the_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    add_history(system, plant, range(2))
    response = _read(_history(system), system, "archived_forecast")
    assert response.status_code == 200
    body = response.json()
    assert body["source_kind"] == "forecast"
    assert body["freshness"] == "archived"
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    assert body["collected_at"] == (start - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    result = body["result"]
    assert result["energy_kwh_m2"] == pytest.approx(9.6)
    assert {row["ghi_wm2"] for row in result["intervals"]} == {400.0}
    provenance = result["provenance"]
    assert provenance["selection_policy"] == "latest-published-pre-daylight-issuance-v1"
    assert provenance["selection_cutoff"] == forecast_cutoff(DAY, system.binding).isoformat()


def test_region_archived_forecast_without_an_issued_forecast_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    response = _read(_history(system), system, "archived_forecast")
    assert response.status_code == 200
    body = response.json()
    assert (body["freshness"], body["unavailable_reason"], body["result"]) == (
        "missing",
        "forecast_unavailable",
        None,
    )


@pytest.mark.parametrize(
    ("source", "offset"),
    [("forecast", -1), ("archived_forecast", 1), ("estimated_actuals", 1)],
)
def test_region_sources_keep_their_date_bounds(
    monkeypatch: pytest.MonkeyPatch, source: str, offset: int
) -> None:
    system, _ = make_plant(monkeypatch)
    client = Client()
    client.force_login(system.member)
    response = _read(client, system, source, TODAY + timedelta(days=offset))
    assert response.status_code == 400
    assert response.json()["code"] in {"date_out_of_range", "completed_day_required"}


@pytest.mark.parametrize("scope", ["plant", "region"])
def test_forecast_reads_default_to_the_automatic_source(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    system, plant = make_plant(monkeypatch)
    add_history(system, plant, range(1))
    client = Client()
    client.force_login(system.member)
    region = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
    url = f"{region}plants/{plant.pk}/forecast/" if scope == "plant" else f"{region}forecast/"
    response = client.get(url, {"date": str(DAY)})
    assert response.status_code == 200
    assert response.json()["source_kind"] == "estimated_actuals"
