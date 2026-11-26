from datetime import date, timedelta

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastResult, ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_archive import forecast_cutoff
from pyracast.regions.models import Region
from tests.support.domain import NOW, System, make_system
from tests.support.invariants import stored_again
from tests.support.plant_operations import publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 7)
NEXT = date(2026, 11, 10)


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


@pytest.mark.parametrize(
    "change",
    [
        "region_revision",
        "disabled_account",
        "rotated_secret",
        "revoked_authorization",
        "pending_region",
        "superseded_binding",
    ],
)
def test_region_forecast_marks_invalidated_binding_stale(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    publish_snapshot(snapshot_for(system, NEXT, collected_at=NOW - timedelta(hours=1)))
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    query = {"date": NEXT.isoformat(), "data_source": "forecast"}
    current = client.get(path, query).json()
    assert (current["freshness"], current["stale_reason"]) == ("fresh", None)
    if change == "region_revision":
        system.region.revision += 1
        system.region.save(update_fields=["revision"])
    elif change == "disabled_account":
        system.account.enabled = False
        system.account.save(update_fields=["enabled"])
    elif change == "rotated_secret":
        system.account.credential_revision += 1
        system.account.save(update_fields=["credential_revision"])
    elif change == "revoked_authorization":
        system.account.authorized_at = None
        system.account.save(update_fields=["authorized_at"])
    elif change == "pending_region":
        system.region.status = "pending"
        system.region.save(update_fields=["status"])
    else:
        system.region.revision += 1
        system.region.approved_binding = SiteBinding.objects.create(
            region=system.region,
            provider_account=system.account,
            region_revision=system.region.revision,
            site_id="replacement",
            capacity_kw=1,
            capacity_semantics_verified=True,
            latitude=system.region.latitude,
            longitude=system.region.longitude,
            timezone=system.region.timezone,
        )
        system.region.save(update_fields=["approved_binding", "revision"])
    response = client.get(path, query)
    assert response.status_code == 200
    if change == "superseded_binding":
        assert (response.json()["freshness"], response.json()["result"]) == ("missing", None)
        return
    assert (response.json()["freshness"], response.json()["stale_reason"]) == (
        "stale",
        "connection_inactive",
    )
    assert response.json()["result"] == current["result"]


@pytest.mark.parametrize(
    "errors", [["Invalid saved irradiance."], {"saved": ["Invalid saved irradiance."]}]
)
def test_region_history_preserves_application_validation_response(
    monkeypatch: pytest.MonkeyPatch, errors: list[str] | dict[str, list[str]]
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    client = Client()
    client.force_login(system.member)

    def invalid_history(*args: object) -> dict[str, object]:
        raise ValidationError(errors)

    monkeypatch.setattr("pyracast.forecasts.outlook.regional_history", invalid_history)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "satellite"})
    assert response.status_code == 400
    assert response.json() == (
        {"code": "invalid_request", "message": "Invalid saved irradiance."}
        if isinstance(errors, list)
        else {
            "code": "invalid_request",
            "message": "Check the highlighted values and try again.",
            "fields": {"saved": "Invalid saved irradiance."},
        }
    )


@pytest.mark.parametrize(
    ("target_date", "source", "code"),
    [
        ("2025-01-01", "forecast", "date_out_of_range"),
        ("2026-11-10", "satellite", "completed_day_required"),
    ],
)
def test_region_outlook_preserves_request_error_codes(
    monkeypatch: pytest.MonkeyPatch, target_date: str, source: str, code: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    response = client.get(path, {"date": target_date, "data_source": source})
    assert response.status_code == 400
    assert response.json()["code"] == code


@pytest.mark.parametrize("future_field", ["collected_at", "issued_at", "published_at"])
def test_region_forecast_excludes_vintages_not_yet_available(
    monkeypatch: pytest.MonkeyPatch, future_field: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    cutoff = forecast_cutoff(DAY, system.binding)
    older = publish_snapshot(snapshot_for(system, DAY, collected_at=cutoff - timedelta(hours=2)))
    newer = snapshot_for(system, DAY, power_kw=0.6, collected_at=cutoff - timedelta(hours=1))
    if future_field == "collected_at":
        newer.collected_at = cutoff + timedelta(seconds=1)
    if future_field == "issued_at":
        newer.issued_at = cutoff
    stored_again(newer)
    publish_snapshot(
        newer, published_at=cutoff if future_field == "published_at" else newer.collected_at
    )
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "forecast"})
    assert response.status_code == 200
    assert response.json()["result"]["provenance"]["snapshot_id"] == older.pk


def test_region_history_keeps_missing_satellite_data_apart_from_a_late_forecast(
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
    satellite = {"date": DAY.isoformat(), "data_source": "satellite"}
    missing = client.get(path, satellite).json()
    assert missing["source_kind"] == "estimated_actuals"
    assert missing["result"] is None
    assert missing["unavailable_reason"] == "estimated_actuals_unavailable"
    actual = actual_for(system)
    response = client.get(path, satellite)
    assert response.status_code == 200
    assert response.json()["source_kind"] == "estimated_actuals"
    assert response.json()["result"]["vintage"]["source_kind"] == "estimated_actuals"
    assert response.json()["collected_at"] == actual.collected_at.isoformat().replace("+00:00", "Z")
    late = client.get(path, {"date": DAY.isoformat(), "data_source": "forecast"}).json()
    assert (late["result"], late["unavailable_reason"]) == (None, "forecast_unavailable")
    assert client.get(path, {"date": "2026-11-10", "data_source": "satellite"}).status_code == 400
    today = client.get(path, {"date": "2026-11-09", "data_source": "satellite"})
    assert today.status_code == 200
    assert today.json()["source_kind"] == "estimated_actuals"
    assert today.json()["result"] is None


def test_region_history_respects_binding_status_and_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    actual_for(system)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    query = {"date": DAY.isoformat(), "data_source": "satellite"}
    assert client.get(path, query).json()["freshness"] == "archived"
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    assert client.get(path, query).json()["freshness"] == "archived"
    Region.objects.filter(pk=system.region.pk).update(
        status="approved", revision=system.region.revision + 1
    )
    assert client.get(path, query).json()["freshness"] == "archived"
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
    other = User.objects.create_user(username="other")
    tenant = Tenant.objects.create(name="Other")
    Membership.objects.create(user=other, tenant=tenant)
    client.force_login(other)
    assert client.get(path, query).status_code == 403
