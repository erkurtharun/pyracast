from datetime import datetime, timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant
from pyracast.plants.models import Plant
from pyracast.regions.models import Region
from tests.support.domain import NOW
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)


def test_trash_restore_hides_all_entity_routes_and_keeps_child_trash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client = Client()
    client.force_login(system.member)
    tenant = f"/api/tenants/{system.tenant.pk}/"
    region = f"{tenant}regions/{system.region.pk}/"
    detail = f"{region}plants/{plant.pk}/"
    assert client.delete(detail).status_code == 204
    assert client.delete(detail).status_code == 204
    assert client.get(region + "plants/").json()["count"] == 0
    for path in ("production/", "calibrations/", "forecast/?date=2026-11-08"):
        assert client.get(detail + path).status_code == 404
    entry = client.get(tenant + "trash/plants/").json()["results"][0]
    assert entry["id"] == plant.pk
    assert entry["can_restore"] is True
    assert entry["parent_deleted"] is False
    assert entry["region_id"] == system.region.pk
    assert entry["region_name"] == system.region.name
    assert datetime.fromisoformat(entry["purge_after"]) == NOW + timedelta(days=30)
    assert client.delete(region).status_code == 204
    assert client.delete(region).status_code == 204
    assert client.get(tenant + "sites/").json()["count"] == 0
    for path in ("plants/", "collection-schedule/", "reminders/", "forecast/?date=2026-11-08"):
        assert client.get(region + path).status_code == 404
    assert client.post(detail + "restore/", {}, content_type="application/json").status_code == 400
    entry = client.get(tenant + "trash/plants/").json()["results"][0]
    assert entry["can_restore"] is False
    assert entry["parent_deleted"] is True
    entry = client.get(tenant + "trash/regions/").json()["results"][0]
    assert entry["can_restore"] is True
    assert datetime.fromisoformat(entry["purge_after"]) == NOW + timedelta(days=30)
    restored = client.post(region + "restore/", {}, content_type="application/json")
    assert restored.status_code == 200
    assert restored.json()["status"] == "approved"
    assert client.get(region + "plants/").json()["count"] == 0
    restored = client.post(detail + "restore/", {}, content_type="application/json")
    assert restored.status_code == 200
    assert restored.json()["current_configuration"]["id"] == plant.current_configuration_id
    assert client.get(region + "plants/").json()["count"] == 1
    assert client.get(tenant + "trash/regions/").json()["count"] == 0
    assert client.get(tenant + "trash/plants/").json()["count"] == 0


def test_expired_trash_remains_visible_but_cannot_be_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client = Client()
    client.force_login(system.member)
    tenant = f"/api/tenants/{system.tenant.pk}/"
    region = f"{tenant}regions/{system.region.pk}/"
    detail = f"{region}plants/{plant.pk}/"
    assert client.delete(detail).status_code == 204
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=30))
    client.force_login(system.member)
    assert client.get(tenant + "trash/plants/").json()["results"][0]["can_restore"] is False
    assert client.post(detail + "restore/", {}, content_type="application/json").status_code == 400
    assert client.delete(region).status_code == 204
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=60))
    client.force_login(system.member)
    assert client.get(tenant + "trash/regions/").json()["results"][0]["can_restore"] is False
    assert client.post(region + "restore/", {}, content_type="application/json").status_code == 400
    assert Plant.objects.filter(pk=plant.pk).exists()
    assert Region.objects.filter(pk=system.region.pk).exists()


def test_trash_and_restore_keep_tenant_boundaries_and_session_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client = Client()
    tenant = f"/api/tenants/{system.tenant.pk}/"
    region = f"{tenant}regions/{system.region.pk}/"
    detail = f"{region}plants/{plant.pk}/"
    assert client.delete(region).status_code == 403
    protected = Client(enforce_csrf_checks=True)
    protected.force_login(system.member)
    assert protected.delete(region).status_code == 403
    assert protected.delete(detail).status_code == 403
    assert protected.post(region + "restore/", {}).status_code == 403
    assert protected.post(detail + "restore/", {}).status_code == 403
    other = Tenant.objects.create(name="Other")
    outsider = User.objects.create_user("trash-outsider")
    Membership.objects.create(tenant=other, user=outsider, role=Membership.Role.ADMIN)
    client.force_login(outsider)
    for path in (region, detail):
        assert client.delete(path).status_code == 403
        assert (
            client.post(path + "restore/", {}, content_type="application/json").status_code == 403
        )
        own = path.replace(f"tenants/{system.tenant.pk}", f"tenants/{other.pk}")
        assert client.delete(own).status_code == 404
        assert client.post(own + "restore/", {}, content_type="application/json").status_code == 404
    for kind in ("regions", "plants"):
        assert client.get(f"{tenant}trash/{kind}/").status_code == 403
        assert client.get(f"/api/tenants/{other.pk}/trash/{kind}/").json()["count"] == 0
    client.force_login(system.member)
    for path in (region, detail):
        assert (
            client.post(
                path + "restore/", {"deleted_at": None}, content_type="application/json"
            ).status_code
            == 400
        )
    assert Region.objects.get(pk=system.region.pk).deleted_at is None
    assert Plant.objects.get(pk=plant.pk).deleted_at is None
