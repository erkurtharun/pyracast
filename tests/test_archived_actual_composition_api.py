from datetime import timedelta
from typing import Any, cast

import pytest
from django.contrib.auth.models import User
from django.test import Client

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import RequestAttempt
from pyracast.irradiance import local_day_bounds
from pyracast.regions.models import Region
from tests.support.archived_actual_composition_api import DAY, acquired_actuals
from tests.support.domain import NOW, System
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("source", ["auto", "estimated_actuals"])
@pytest.mark.parametrize("scope", ["region", "plant"])
def test_completed_day_api_composes_actuals_acquired_during_that_day(
    monkeypatch: pytest.MonkeyPatch, source: str, scope: str
) -> None:
    system, plant = make_plant(monkeypatch)
    morning = acquired_actuals(system, 7, 12, 0.1)
    afternoon = acquired_actuals(system, 12, 18, 0.2)
    start, end = local_day_bounds(DAY, system.binding.timezone)
    assert start < morning.collected_at < afternoon.collected_at < end
    client = Client()
    client.force_login(system.member)
    base = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
    path = base + (f"plants/{plant.pk}/forecast/" if scope == "plant" else "forecast/")
    attempts = RequestAttempt.objects.count()
    response = client.get(path, {"date": DAY.isoformat(), "data_source": source})
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["source_kind"] == "estimated_actuals"
    result = body["result"]
    assert result is not None
    daylight = result["daylight"]
    assert daylight["complete"] is True
    assert daylight["coverage_fraction"] == 1
    energy_key = "energy_mwh" if scope == "plant" else "energy_kwh_m2"
    assert daylight[energy_key] == pytest.approx(214.5 if scope == "plant" else 1.65)
    assert result[energy_key] is None
    assert set(result["provenance"]["source_snapshot_ids"]) == {morning.pk, afternoon.pk}
    assert result["provenance"]["source_kind"] == "estimated_actuals"
    assert {item["snapshot_id"] for item in result["provenance"]["source_snapshots"]} == {
        morning.pk,
        afternoon.pk,
    }
    assert RequestAttempt.objects.count() == attempts


@pytest.mark.parametrize("future_field", ["available_at", "issued_at", "collected_at"])
def test_archived_actual_composition_excludes_later_publication_and_other_tenant(
    monkeypatch: pytest.MonkeyPatch, future_field: str
) -> None:
    system, plant = make_plant(monkeypatch)
    morning = acquired_actuals(system, 7, 12, 0.1)
    afternoon = acquired_actuals(system, 12, 18, 0.2)
    future = acquired_actuals(system, 7, 18, 0.9)
    setattr(future, future_field, NOW + timedelta(seconds=1))
    future.save(update_fields=(future_field,))
    tenant = Tenant.objects.create(name="Other tenant")
    other = User.objects.create_user(username="other")
    Membership.objects.create(user=other, tenant=tenant, role="admin")
    region = Region.objects.create(
        tenant=tenant,
        name="Other region",
        latitude=40,
        longitude=29,
        timezone=system.binding.timezone,
    )
    binding = SiteBinding.objects.create(
        region=region,
        provider_account=system.account,
        region_revision=1,
        site_id="other",
        capacity_kw=1,
        latitude=40,
        longitude=29,
        timezone=system.binding.timezone,
    )
    outsider = acquired_actuals(
        System(other, system.admin, tenant, region, system.account, binding), 7, 18, 0.8
    )
    revision = SiteBinding.objects.create(
        region=system.region,
        provider_account=system.account,
        region_revision=system.region.revision + 1,
        site_id="other-revision",
        capacity_kw=1,
        latitude=40,
        longitude=29,
        timezone=system.binding.timezone,
    )
    other_revision = acquired_actuals(
        System(system.member, system.admin, system.tenant, system.region, system.account, revision),
        7,
        18,
        0.7,
    )
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "auto"})
    assert response.status_code == 200, response.content
    result = cast(dict[str, Any], response.json()["result"])
    assert result["daylight"]["energy_mwh"] == pytest.approx(214.5)
    assert set(result["provenance"]["source_snapshot_ids"]) == {morning.pk, afternoon.pk}
    assert future.pk not in result["provenance"]["source_snapshot_ids"]
    assert outsider.pk not in result["provenance"]["source_snapshot_ids"]
    assert other_revision.pk not in result["provenance"]["source_snapshot_ids"]
    client.force_login(other)
    assert client.get(path, {"date": DAY.isoformat(), "data_source": "auto"}).status_code == 403
