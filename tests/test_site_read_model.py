from typing import Any

import pytest
from django.test import Client

from pyracast import trash
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)


def _get(client: Client, url: str) -> Any:
    response = client.get(url)
    assert response.status_code == 200
    return response.json()


def test_site_and_plant_rows_carry_no_provider_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    client = Client()
    client.force_login(system.member)
    tenant = f"/api/tenants/{system.tenant.pk}/"
    [site] = _get(client, f"{tenant}sites/")["results"]
    assert site["id"] == system.region.pk
    assert site["plant"]["id"] == plant.pk
    assert site["plant"]["current_configuration"]["id"] == plant.current_configuration_id
    assert "binding_id" not in site["plant"]["current_configuration"]
    [row] = _get(client, f"{tenant}regions/{system.region.pk}/plants/")["results"]
    assert "binding_id" not in row["current_configuration"]


def test_a_trashed_site_leaves_the_site_list(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _ = make_plant(monkeypatch)
    client = Client()
    client.force_login(system.member)
    trash.trash_region(system.member, system.tenant.pk, system.region.pk)
    assert _get(client, f"/api/tenants/{system.tenant.pk}/sites/")["results"] == []
