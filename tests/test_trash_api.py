from datetime import datetime, timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from pyracast import trash
from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.plants.models import Plant, PlantEvent
from pyracast.regions.models import Region
from tests.support.domain import NOW
from tests.support.plant_operations import make_plant
from tests.support.trash_operations import sibling

pytestmark = pytest.mark.django_db(transaction=True)


def test_trash_restore_hides_all_location_and_plant_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client = Client()
    client.force_login(system.member)
    tenant = f"/api/tenants/{system.tenant.pk}/"
    region = f"{tenant}regions/{system.region.pk}/"
    detail = f"{region}plants/{plant.pk}/"
    assert client.delete(region).status_code == 204
    assert client.delete(region).status_code == 204
    assert client.get(tenant + "sites/").json()["count"] == 0
    for path in ("plants/", "collection-schedule/", "reminders/", "forecast/?date=2026-11-08"):
        assert client.get(region + path).status_code == 404
    for path in ("production/", "calibrations/", "forecast/?date=2026-11-08"):
        assert client.get(detail + path).status_code == 404
    entry = client.get(tenant + "trash/regions/").json()["results"][0]
    assert entry["restore_blocker"] is None
    assert datetime.fromisoformat(entry["purge_after"]) == NOW + timedelta(days=30)
    restored = client.post(region + "restore/", {}, content_type="application/json")
    assert restored.status_code == 200
    assert restored.json()["status"] == "approved"
    assert client.get(region + "plants/").json()["count"] == 1
    assert client.get(tenant + "trash/regions/").json()["count"] == 0


def test_a_plant_leaves_the_workspace_and_returns_only_with_its_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client = Client()
    client.force_login(system.member)
    tenant = f"/api/tenants/{system.tenant.pk}/"
    region = f"{tenant}regions/{system.region.pk}/"
    detail = f"{region}plants/{plant.pk}/"
    assert client.delete(detail).status_code == 405
    assert client.post(detail + "restore/", {}, content_type="application/json").status_code == 404
    assert client.get(tenant + "trash/plants/").status_code == 404
    assert client.get(region + "plants/").json()["count"] == 1
    assert client.delete(region).status_code == 204
    assert client.get(region + "plants/").status_code == 404
    restored = client.post(region + "restore/", {}, content_type="application/json")
    assert restored.status_code == 200
    rows = client.get(region + "plants/").json()["results"]
    assert [(row["id"], row["current_configuration"]["id"]) for row in rows] == [
        (plant.pk, plant.current_configuration_id)
    ]
    assert not PlantEvent.objects.filter(action__in=("plant_trashed", "plant_restored")).exists()


def test_expired_trash_remains_visible_but_cannot_be_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client = Client()
    client.force_login(system.member)
    tenant = f"/api/tenants/{system.tenant.pk}/"
    region = f"{tenant}regions/{system.region.pk}/"
    assert client.delete(region).status_code == 204
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=30))
    client.force_login(system.member)
    listed = client.get(tenant + "trash/regions/").json()["results"][0]
    assert listed["restore_blocker"] == "trash_retention_expired"
    assert client.post(region + "restore/", {}, content_type="application/json").status_code == 400
    assert Plant.objects.filter(pk=plant.pk).exists()
    assert Region.objects.filter(pk=system.region.pk).exists()


def test_a_partly_purged_region_leaves_the_trash_and_keeps_its_deletion_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    waiting, _ = sibling(system)
    client = Client()
    client.force_login(system.member)
    tenant = f"/api/tenants/{system.tenant.pk}/"
    region = f"{tenant}regions/{system.region.pk}/"
    assert client.delete(region).status_code == 204
    assert client.delete(f"{tenant}regions/{waiting.pk}/").status_code == 204
    Region.objects.filter(pk=waiting.pk).update(deleted_at=NOW + timedelta(days=1))
    monkeypatch.setattr("pyracast.trash.PURGE_STATEMENT_BUDGET", 1)
    trash.purge_expired(NOW + trash.RETENTION)
    partial = Region.objects.get(pk=system.region.pk)
    assert (partial.deleted_at, partial.purge_started_at) == (NOW, NOW + trash.RETENTION)

    monkeypatch.setattr(timezone, "now", lambda: NOW - timedelta(days=365))
    client.force_login(system.member)
    refused = client.post(region + "restore/", {}, content_type="application/json")
    assert refused.status_code == 400
    assert refused.json()["code"] == "trash_retention_expired"
    listing = client.get(tenant + "trash/regions/").json()
    assert (listing["count"], [row["id"] for row in listing["results"]]) == (1, [waiting.pk])
    assert Region.objects.get(pk=system.region.pk).deleted_at == NOW


def test_trash_and_restore_keep_tenant_boundaries_and_session_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    client = Client()
    tenant = f"/api/tenants/{system.tenant.pk}/"
    region = f"{tenant}regions/{system.region.pk}/"
    assert client.delete(region).status_code == 401
    protected = Client(enforce_csrf_checks=True)
    protected.force_login(system.member)
    assert protected.delete(region).status_code == 403
    assert protected.post(region + "restore/", {}).status_code == 403
    other = Tenant.objects.create(name="Other")
    outsider = User.objects.create_user("trash-outsider")
    Membership.objects.create(tenant=other, user=outsider, role=WorkspaceRole.ADMIN)
    client.force_login(outsider)
    assert client.delete(region).status_code == 403
    assert client.post(region + "restore/", {}, content_type="application/json").status_code == 403
    own = region.replace(f"tenants/{system.tenant.pk}", f"tenants/{other.pk}")
    assert client.delete(own).status_code == 404
    assert client.post(own + "restore/", {}, content_type="application/json").status_code == 404
    assert client.get(f"{tenant}trash/regions/").status_code == 403
    assert client.get(f"/api/tenants/{other.pk}/trash/regions/").json()["count"] == 0
    client.force_login(system.member)
    assert (
        client.post(
            region + "restore/", {"deleted_at": None}, content_type="application/json"
        ).status_code
        == 400
    )
    assert Region.objects.get(pk=system.region.pk).deleted_at is None


def test_a_bin_row_carries_the_plant_name_and_location_of_the_deleted_plant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    location, _ = sibling(system)
    Region.objects.filter(pk=system.region.pk).update(name="Location name")
    client = Client()
    client.force_login(system.member)
    tenant = f"/api/tenants/{system.tenant.pk}/"
    assert client.delete(f"{tenant}regions/{system.region.pk}/").status_code == 204
    assert client.delete(f"{tenant}regions/{location.pk}/").status_code == 204
    rows = {row["id"]: row for row in client.get(tenant + "trash/regions/").json()["results"]}
    assert rows[system.region.pk]["plant_name"] == plant.name
    assert rows[location.pk]["plant_name"] == location.name
    assert (rows[system.region.pk]["latitude"], rows[system.region.pk]["longitude"]) == (40, 29)
