from datetime import date, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastResult, ForecastSnapshot, RequestAttempt
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import PlantConfiguration
from pyracast.plants.operations import revise_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.models import Region
from pyracast.solar import daylight_window
from tests.support.automatic_forecast_selection import reference_transition
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant, publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 14)


@pytest.mark.parametrize("earlier_forecast", [False, True])
def test_region_rechecks_publication_when_binding_timezone_narrows_saved_cutoff(
    monkeypatch: pytest.MonkeyPatch, earlier_forecast: bool
) -> None:
    system, _plant = make_plant(monkeypatch)
    start, end = local_day_bounds(DAY, system.binding.timezone)
    earlier = (
        publish_snapshot(
            snapshot_for(system, DAY, power_kw=0.1, collected_at=start + timedelta(hours=10))
        )
        if earlier_forecast
        else None
    )
    publish_snapshot(snapshot_for(system, DAY, power_kw=0.9, collected_at=end + timedelta(hours=1)))
    Region.objects.filter(pk=system.region.pk).update(timezone="UTC")
    monkeypatch.setattr(timezone, "now", lambda: end + timedelta(hours=4))
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    requests = RequestAttempt.objects.count()
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "auto"})
    assert response.status_code == 200, response.content
    payload = response.json()
    if earlier is None:
        assert payload["source_kind"] == "estimated_actuals"
        assert payload["result"] is None
        assert payload["unavailable_reason"] == "estimated_actuals_unavailable"
    else:
        assert payload["source_kind"] == "forecast"
        assert payload["result"]["provenance"]["source_snapshot_ids"] == [earlier.pk]
        assert (
            payload["result"]["provenance"]["selection_cutoff"]
            == (end - timedelta(microseconds=1)).isoformat()
        )
    assert RequestAttempt.objects.count() == requests


def test_saved_fallback_does_not_assume_a_configuration_after_its_binding_day_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, end = local_day_bounds(DAY, system.binding.timezone)
    publish_snapshot(
        snapshot_for(system, DAY, power_kw=0.1, collected_at=start + timedelta(hours=10))
    )
    PlantConfiguration.objects.filter(plant=plant).update(created_at=end + timedelta(hours=1))
    Region.objects.filter(pk=system.region.pk).update(timezone="UTC")
    monkeypatch.setattr(timezone, "now", lambda: end + timedelta(hours=4))
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "auto"})
    assert response.status_code == 200, response.content
    assert response.json()["source_kind"] == "estimated_actuals"
    assert response.json()["result"] is None
    assert response.json()["unavailable_reason"] == "estimated_actuals_unavailable"


@pytest.mark.parametrize("error_type", [ValueError, ArithmeticError])
def test_saved_actuals_calculation_failure_returns_validation_error_without_forecast_write(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    system, plant = make_plant(monkeypatch)
    _, end = local_day_bounds(DAY, system.binding.timezone)
    publish_snapshot(snapshot_for(system, DAY, source_kind="estimated_actuals", collected_at=end))
    monkeypatch.setattr(timezone, "now", lambda: end + timedelta(hours=1))

    def fail_calculation(_snapshot: ForecastSnapshot, _date: date) -> dict[str, object]:
        raise error_type("Saved input cannot be calculated.")

    monkeypatch.setattr("pyracast.plants.history.historical_irradiance", fail_calculation)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    requests = RequestAttempt.objects.count()
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "auto"})
    assert response.status_code == 400, response.content
    assert response.json() == {
        "code": "calculation_out_of_range",
        "message": (
            "This forecast could not be calculated. "
            "Choose another day or contact your administrator."
        ),
    }
    assert RequestAttempt.objects.count() == requests


@pytest.mark.parametrize("scope", ["plant", "region"])
def test_automatic_keeps_intraday_forecast_until_actual_values_arrive(
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

    def read(source: str = "auto") -> dict[str, Any]:
        response = client.get(path, {"date": DAY.isoformat(), "data_source": source})
        assert response.status_code == 200, response.content
        return response.json()  # type: ignore[no-any-return]

    before = read()
    requests = RequestAttempt.objects.count()
    result_rows = list(ForecastResult.objects.values())
    monkeypatch.setattr(timezone, "now", lambda: end)
    after = read()
    assert after["source_kind"] == "forecast"
    assert after["result"] is not None
    assert after["result"]["intervals"] == before["result"]["intervals"]
    assert after["result"]["provenance"]["source_snapshot_ids"] == [latest.pk, first.pk]
    assert after["result"]["provenance"]["fallback_reason"] == "estimated_actuals_unavailable"
    assert (
        after["result"]["provenance"]["selection_cutoff"]
        == (end - timedelta(microseconds=1)).isoformat()
    )
    assert "forecast_fallback" in after["result"]["quality_flags"]
    assert read("estimated_actuals")["result"] is None
    if scope == "plant":
        assert after["result"]["energy_mwh"] == pytest.approx(468)
        archived = read("archived_forecast")
        assert archived["result"]["provenance"]["snapshot_id"] == first.pk
        assert archived["result"]["energy_mwh"] == pytest.approx(312)
    assert list(ForecastResult.objects.values()) == result_rows
    assert RequestAttempt.objects.count() == requests

    publish_snapshot(snapshot_for(system, DAY, power_kw=0.9, collected_at=end))
    monkeypatch.setattr(timezone, "now", lambda: end + timedelta(minutes=30))
    assert read()["result"] == after["result"]
    actual = snapshot_for(system, DAY, source_kind="estimated_actuals", collected_at=end)
    for row in actual.normalized_intervals:
        row["power_kw"] = None
    publish_snapshot(actual)
    assert read()["source_kind"] == "forecast"
    actual.normalized_intervals[0]["power_kw"] = 0
    stored_again(actual)
    assert read()["source_kind"] == "forecast"
    actual.normalized_intervals[24]["power_kw"] = 0.2
    stored_again(actual)
    assert read()["source_kind"] == "forecast"
    partial = read("estimated_actuals")
    assert partial["source_kind"] == "estimated_actuals"
    assert partial["result"]["complete"] is False
    daylight = daylight_window(
        DAY, system.binding.timezone, system.binding.latitude, system.binding.longitude
    )
    for row in actual.normalized_intervals:
        begin, stop = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
        row["power_kw"] = (
            0 if any(begin < right and stop > left for left, right in daylight.windows) else None
        )
    stored_again(actual)
    arrived = read()
    assert arrived["source_kind"] == "estimated_actuals"
    assert arrived["result"]["complete"] is False
    if scope == "region":
        assert arrived["result"]["daylight"]["input_coverage_fraction"] == 1
    else:
        assert arrived["result"]["daylight"]["complete"] is True
    assert "forecast_fallback" not in arrived["result"]["quality_flags"]
    assert RequestAttempt.objects.count() == requests


def test_saved_fallback_keeps_day_end_configuration_and_tenant_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, end = local_day_bounds(DAY, system.binding.timezone)
    previous_id = plant.current_configuration_id
    publish_snapshot(
        snapshot_for(system, DAY, power_kw=0.1, collected_at=start + timedelta(hours=10))
    )
    other_binding = SiteBinding.objects.get(pk=system.binding.pk)
    other_binding.pk = None
    other_binding.region_revision += 1
    other_binding.site_id = "different-provider-site"
    other_binding.save()
    original_binding = system.binding
    system.binding = other_binding
    publish_snapshot(
        snapshot_for(system, DAY, power_kw=0.9, collected_at=start + timedelta(hours=12))
    )
    system.binding = original_binding
    monkeypatch.setattr(timezone, "now", lambda: end + timedelta(minutes=1))
    Region.objects.filter(pk=system.region.pk).update(
        approved_binding=other_binding, revision=other_binding.region_revision
    )
    revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        PlantParameters(name="Revised plant", dc_capacity_mw=260, export_limit_mw=100),
    )
    Region.objects.filter(pk=system.region.pk).update(timezone="UTC")
    monkeypatch.setattr(timezone, "now", lambda: end + timedelta(hours=4))
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "auto"})
    assert response.status_code == 200, response.content
    assert response.json()["configuration_id"] == previous_id
    assert response.json()["result"]["energy_mwh"] == pytest.approx(312)
    assert response.json()["result"]["provenance"]["binding_id"] == original_binding.pk
    assert "previous_configuration" in response.json()["result"]["quality_flags"]
    outsider = User.objects.create(username="other-member")
    other_tenant = Tenant.objects.create(name="Other tenant")
    Membership.objects.create(user=outsider, tenant=other_tenant)
    client.force_login(outsider)
    assert client.get(path, {"date": DAY.isoformat(), "data_source": "auto"}).status_code == 403


@pytest.mark.parametrize("scope", ["plant", "region"])
def test_missing_forecast_and_actuals_remain_unavailable(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    system, plant = make_plant(monkeypatch)
    _, end = local_day_bounds(DAY, system.binding.timezone)
    monkeypatch.setattr(timezone, "now", lambda: end)
    client = Client()
    client.force_login(system.member)
    prefix = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}"
    path = f"{prefix}/plants/{plant.pk}/forecast/" if scope == "plant" else f"{prefix}/forecast/"
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "auto"})
    assert response.status_code == 200, response.content
    assert response.json()["source_kind"] == "estimated_actuals"
    assert response.json()["result"] is None
    assert response.json()["unavailable_reason"] == "estimated_actuals_unavailable"
    assert not ForecastSnapshot.objects.exists()
    actual = snapshot_for(system, DAY, source_kind="estimated_actuals", collected_at=end)
    actual.normalized_intervals = actual.normalized_intervals[24:25]
    publish_snapshot(actual)
    partial = client.get(path, {"date": DAY.isoformat(), "data_source": "auto"}).json()
    assert partial["source_kind"] == "estimated_actuals"
    assert partial["result"]["complete"] is False


def test_reference_transition_fallback_survives_local_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, original_id = reference_transition(monkeypatch)
    day = date(2026, 11, 11)
    _, end = local_day_bounds(day, system.binding.timezone)
    monkeypatch.setattr(timezone, "now", lambda: end - timedelta(microseconds=1))
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    query = {"date": day.isoformat(), "data_source": "auto"}
    before = client.get(path, query).json()
    monkeypatch.setattr(timezone, "now", lambda: end)
    response = client.get(path, query)
    assert response.status_code == 200, response.content
    after = response.json()
    assert after["configuration_id"] == before["configuration_id"]
    assert after["result"]["energy_mwh"] == before["result"]["energy_mwh"] == pytest.approx(312)
    assert after["result"]["provenance"]["snapshot_id"] == original_id
    assert "reference_transition_fallback" in after["result"]["quality_flags"]
    assert "forecast_fallback" in after["result"]["quality_flags"]


@pytest.mark.parametrize("scope", ["plant", "region"])
def test_polar_night_actuals_require_complete_local_day(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    system, plant = make_plant(monkeypatch)
    SiteBinding.objects.filter(pk=system.binding.pk).update(latitude=80, longitude=20)
    Region.objects.filter(pk=system.region.pk).update(latitude=80, longitude=20)
    system.binding.refresh_from_db()
    start, end = local_day_bounds(DAY, system.binding.timezone)
    publish_snapshot(
        snapshot_for(system, DAY, power_kw=0.1, collected_at=start + timedelta(hours=3))
    )
    actual = snapshot_for(
        system, DAY, power_kw=0, source_kind="estimated_actuals", collected_at=end
    )
    actual.normalized_intervals[-1]["power_kw"] = None
    publish_snapshot(actual)
    monkeypatch.setattr(timezone, "now", lambda: end)
    client = Client()
    client.force_login(system.member)
    prefix = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}"
    path = f"{prefix}/plants/{plant.pk}/forecast/" if scope == "plant" else f"{prefix}/forecast/"
    query = {"date": DAY.isoformat(), "data_source": "auto"}
    assert client.get(path, query).json()["source_kind"] == "forecast"
    actual.normalized_intervals[-1]["power_kw"] = 0
    stored_again(actual)
    response = client.get(path, query)
    assert response.status_code == 200, response.content
    assert response.json()["source_kind"] == "estimated_actuals"
    assert response.json()["result"]["daylight"]["status"] == "polar_night"
