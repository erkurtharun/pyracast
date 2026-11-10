from datetime import date, timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastResult, ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.regions.models import Region
from tests.test_domain import NOW, System, make_system
from tests.test_plant_operations import snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 7)


def actual_for(system: System) -> ForecastSnapshot:
    start, end = local_day_bounds(DAY, system.binding.timezone)
    snapshot = snapshot_for(
        system, DAY, collected_at=end + timedelta(hours=1), source_kind="estimated_actuals"
    )
    snapshot.coverage_start, snapshot.coverage_end = start, end
    snapshot.available_at = snapshot.collected_at
    snapshot.save()
    snapshot.job.endpoint, snapshot.job.status = "estimated_actuals", "succeeded"
    snapshot.job.save()
    return snapshot


@pytest.mark.parametrize("future_field", ["collected_at", "issued_at", "published_at"])
def test_region_forecast_excludes_vintages_not_yet_available(
    monkeypatch: pytest.MonkeyPatch, future_field: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    older = snapshot_for(system, DAY, collected_at=NOW - timedelta(hours=2))
    ForecastResult.objects.create(snapshot=older, payload={"marker": "available"}, published_at=NOW)
    newer = snapshot_for(system, DAY, collected_at=NOW - timedelta(hours=1))
    if future_field in {"collected_at", "issued_at"}:
        setattr(newer, future_field, NOW + timedelta(seconds=1))
        newer.save(update_fields=[future_field])
    ForecastResult.objects.create(
        snapshot=newer,
        payload={"marker": "future"},
        published_at=NOW + timedelta(seconds=1) if future_field == "published_at" else NOW,
    )
    for source in ("forecast", "auto"):
        response = client.get(path, {"date": DAY.isoformat(), "data_source": source})
        assert response.status_code == 200
        assert response.json()["result"] == {"marker": "available"}


def test_region_history_prefers_actuals_but_preserves_forecast_and_missing_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    forecast = snapshot_for(system, DAY)
    ForecastResult.objects.create(
        snapshot=forecast, payload={"marker": "archive"}, published_at=NOW
    )
    assert client.get(path, {"date": DAY.isoformat(), "data_source": "auto"}).json()["result"] == {
        "marker": "archive"
    }
    missing = client.get(path, {"date": DAY.isoformat(), "data_source": "estimated_actuals"}).json()
    assert missing["result"] is None
    assert missing["unavailable_reason"] == "estimated_actuals_unavailable"
    actual = actual_for(system)
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "auto"})
    assert response.status_code == 200
    assert response.json()["source_kind"] == "estimated_actuals"
    assert response.json()["result"]["vintage"]["source_kind"] == "estimated_actuals"
    assert response.json()["collected_at"] == actual.collected_at.isoformat().replace("+00:00", "Z")
    archive = client.get(path, {"date": DAY.isoformat(), "data_source": "forecast"}).json()
    assert archive["source_kind"] == "forecast"
    assert archive["result"] == {"marker": "archive"}
    assert (
        client.get(path, {"date": "2026-11-10", "data_source": "estimated_actuals"}).status_code
        == 400
    )
    assert (
        client.get(path, {"date": "2026-11-09", "data_source": "estimated_actuals"}).status_code
        == 400
    )


def test_region_history_respects_binding_status_and_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    actual_for(system)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    query = {"date": DAY.isoformat(), "data_source": "estimated_actuals"}
    assert client.get(path, query).json()["freshness"] == "fresh"
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    assert client.get(path, query).json()["freshness"] == "stale"
    Region.objects.filter(pk=system.region.pk).update(
        status="approved", revision=system.region.revision + 1
    )
    assert client.get(path, query).json()["freshness"] == "stale"
    replacement = SiteBinding.objects.create(
        region=system.region,
        provider_account=system.account,
        region_revision=system.region.revision + 1,
        site_id="replacement",
        capacity_kw=1,
        capacity_semantics_verified=True,
        latitude=system.region.latitude,
        longitude=system.region.longitude,
        timezone=system.region.timezone,
    )
    Region.objects.filter(pk=system.region.pk).update(approved_binding=replacement)
    assert client.get(path, query).json()["freshness"] == "missing"
    Region.objects.filter(pk=system.region.pk).update(status="approved", approved_binding=None)
    missing = client.get(path, query).json()
    assert missing["freshness"] == "missing"
    assert missing["result"] is None
    other = User.objects.create_user(username="other")
    tenant = Tenant.objects.create(name="Other")
    Membership.objects.create(user=other, tenant=tenant)
    client.force_login(other)
    assert client.get(path, query).status_code == 403
