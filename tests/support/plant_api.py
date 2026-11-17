from datetime import datetime
from typing import Any

from django.test import Client

from tests.support.domain import make_system
from tests.support.plant_operations import saved_actuals


def setup_plant() -> tuple[Client, str, dict[str, Any]]:
    system = make_system(approved=True)
    saved_actuals(system, datetime(2020, 1, 1).date())
    client = Client()
    client.force_login(system.member)
    base = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/"
    response = client.post(
        base, {"name": "PV", "dc_capacity_mw": 1}, content_type="application/json"
    )
    assert response.status_code == 201, response.content
    return client, base, response.json()
