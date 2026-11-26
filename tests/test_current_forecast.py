from datetime import date, timedelta
from typing import Any, cast

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import ProviderAccount
from pyracast.forecasts.models import ForecastResult, RequestAttempt
from pyracast.irradiance import local_day_bounds
from pyracast.plants.activation import activate_calibration
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import ForecastIssuance, Plant, PlantConfiguration
from pyracast.regions.models import Region
from tests.support.domain import NOW
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 10)


def test_daily_acquisitions_refresh_live_outlook_and_keep_day_ahead_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    snapshots = []
    requests = RequestAttempt.objects.count()
    for hour, power in ((6, 0.1), (7, 0.2), (8, 0.3)):
        acquired = NOW.replace(hour=hour)
        monkeypatch.setattr(timezone, "now", lambda acquired=acquired: acquired)
        snapshot = publish_snapshot(
            snapshot_for(system, DAY, power_kw=power, collected_at=acquired)
        )
        snapshots.append(snapshot)
        result = plant_outlook(*scope, target_date=DAY, resolution_minutes=60)
        payload = cast(dict[str, Any], result["result"])
        assert result["as_of"] == acquired.isoformat()
        assert payload["provenance"]["snapshot_id"] == snapshot.pk
        assert payload["energy_mwh"] == pytest.approx(power * 3120)
        assert payload["intervals"][9]["mean_power_mw"] == pytest.approx(power * 130)
    assert payload["provenance"]["selection_policy"] == "latest-forecast-coverage-v2"
    archived = plant_outlook(
        *scope, target_date=DAY, resolution_minutes=60, forecast_id=snapshots[-1].pk
    )
    archived_payload = cast(dict[str, Any], archived["result"])
    assert archived_payload["provenance"]["snapshot_id"] == snapshots[-1].pk
    assert (
        archived_payload["provenance"]["selection_policy"]
        == "latest-published-pre-daylight-issuance-v1"
    )
    assert archived_payload["energy_mwh"] == pytest.approx(936)
    assert ForecastIssuance.objects.count() == 3
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=2))
    past = plant_outlook(*scope, target_date=DAY, resolution_minutes=60)
    assert past["result"] == archived_payload
    assert RequestAttempt.objects.count() == requests


def test_today_uses_latest_collection_without_changing_midnight_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    today = NOW.date()
    original = publish_snapshot(snapshot_for(system, today, power_kw=0.1))
    latest = publish_snapshot(snapshot_for(system, today, power_kw=0.2, collected_at=NOW))
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    current = plant_outlook(*scope, target_date=today, resolution_minutes=30)
    assert cast(dict[str, Any], current["result"])["provenance"]["snapshot_id"] == latest.pk
    archived = plant_outlook(
        *scope, target_date=today, resolution_minutes=30, forecast_id=original.pk
    )
    assert cast(dict[str, Any], archived["result"])["provenance"]["snapshot_id"] == original.pk


@pytest.mark.parametrize(
    "change",
    [
        "collected",
        "issued",
        "available",
        "unpublished",
        "publication",
        "pending",
        "actuals",
        "missing_available",
        "missing_coverage",
        "before_coverage",
        "after_coverage",
    ],
)
def test_live_selection_excludes_unavailable_and_unmatched_snapshots(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    system, plant = make_plant(monkeypatch)
    original = publish_snapshot(snapshot_for(system, DAY, collected_at=NOW - timedelta(hours=2)))
    latest = publish_snapshot(snapshot_for(system, DAY, collected_at=NOW - timedelta(hours=1)))
    if change == "collected":
        latest.collected_at = NOW + timedelta(seconds=1)
    elif change == "issued":
        latest.issued_at = NOW + timedelta(seconds=1)
    elif change == "available":
        latest.available_at = NOW + timedelta(seconds=1)
    elif change == "missing_available":
        latest.available_at = None
    elif change == "unpublished":
        ForecastResult.objects.filter(snapshot=latest).delete()
    elif change == "publication":
        ForecastResult.objects.filter(snapshot=latest).update(
            published_at=NOW + timedelta(seconds=1)
        )
    elif change == "pending":
        latest.job.status = "pending"
        latest.job.save(update_fields=["status"])
    elif change == "actuals":
        latest.source_kind = "estimated_actuals"
    elif change == "missing_coverage":
        latest.coverage_start = latest.coverage_end = None
    elif change == "before_coverage":
        latest.coverage_start = local_day_bounds(DAY, system.binding.timezone)[1]
        latest.coverage_end = latest.coverage_start + timedelta(minutes=30)
    else:
        latest.coverage_end = local_day_bounds(DAY, system.binding.timezone)[0]
        latest.coverage_start = latest.coverage_end - timedelta(minutes=30)
    stored_again(latest)
    result = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    assert cast(dict[str, Any], result["result"])["provenance"]["snapshot_id"] == original.pk


def test_live_source_can_cover_a_different_collection_target_and_keeps_latest_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, DAY, collected_at=NOW - timedelta(hours=1)))
    latest = snapshot_for(system, DAY, collected_at=NOW, power_kw=0.2)
    latest.job.target_date = DAY - timedelta(days=1)
    latest.job.save(update_fields=["target_date"])
    latest.normalized_intervals[0]["power_kw"] = None
    publish_snapshot(latest)
    result = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=30,
    )
    payload = cast(dict[str, Any], result["result"])
    assert payload["provenance"]["snapshot_id"] == latest.pk
    assert payload["intervals"][0]["mean_power_mw"] is None
    assert payload["energy_mwh"] is None
    assert payload["partial_energy_mwh"] == pytest.approx(611)
    assert cast(dict[str, Any], result["regional_irradiance"])["local_date"] == DAY.isoformat()


def test_new_current_configuration_and_calibration_are_available_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    PlantConfiguration.objects.filter(plant=plant).update(created_at=NOW)
    run = ready_run(system, plant)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    activate_calibration(*scope, run.pk)
    publish_snapshot(snapshot_for(system, NOW.date(), collected_at=NOW))
    result = plant_outlook(*scope, target_date=NOW.date(), resolution_minutes=60)
    payload = cast(dict[str, Any], result["result"])
    assert result["configuration_id"] == plant.current_configuration_id
    assert result["calibration_id"] == run.pk
    assert payload["factor"] == 0.5
    assert "historical_configuration_assumed" not in payload["quality_flags"]


@pytest.mark.parametrize("change", ["unconfigured", "future", "suspended", "disabled", "binding"])
def test_live_requires_current_approved_configuration(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, DAY, collected_at=NOW))
    if change == "unconfigured":
        Plant.objects.filter(pk=plant.pk).update(current_configuration=None)
    elif change == "future":
        PlantConfiguration.objects.filter(plant=plant).update(created_at=NOW + timedelta(seconds=1))
    elif change == "suspended":
        Region.objects.filter(pk=system.region.pk).update(status="suspended")
    elif change == "disabled":
        ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=False)
    else:
        Region.objects.filter(pk=system.region.pk).update(status="pending", approved_binding=None)
    result = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    assert result["unavailable_reason"] == "configuration_unavailable"
    assert result["result"] is None


def test_live_forecast_api_keeps_tenant_boundary_and_rejects_corrupt_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot = publish_snapshot(snapshot_for(system, DAY, collected_at=NOW))
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    assert (
        client.get(path, {"date": DAY.isoformat()}).json()["result"]["provenance"]["snapshot_id"]
        == snapshot.pk
    )
    snapshot.normalized_intervals[0]["power_kw"] = -1
    stored_again(snapshot)
    assert client.get(path, {"date": DAY.isoformat()}).status_code == 400
    tenant = Tenant.objects.create(name="Other")
    user = User.objects.create_user("other-tenant")
    Membership.objects.create(tenant=tenant, user=user)
    client.force_login(user)
    assert client.get(path, {"date": DAY.isoformat()}).status_code == 403
    other_path = path.replace(f"tenants/{system.tenant.pk}", f"tenants/{tenant.pk}")
    assert client.get(other_path, {"date": DAY.isoformat()}).status_code == 404


@pytest.mark.parametrize(
    "values",
    [
        {"resolution_minutes": True},
        {"resolution_minutes": 15},
        {"target_date": DAY + timedelta(days=15)},
    ],
)
def test_live_validates_resolution_and_date_range(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]
) -> None:
    system, plant = make_plant(monkeypatch)
    arguments: dict[str, Any] = {"target_date": DAY, "resolution_minutes": 60} | values
    with pytest.raises(ValidationError):
        plant_outlook(system.member, system.tenant.pk, system.region.pk, plant.pk, **arguments)


def test_forecast_selection_does_not_fall_back_to_actuals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, DAY, source_kind="estimated_actuals", collected_at=NOW))
    result = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    assert result["unavailable_reason"] == "forecast_unavailable"
    assert result["result"] is None


def test_older_covering_forecast_is_explicitly_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, DAY, collected_at=NOW - timedelta(hours=25)))
    result = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    assert result["freshness"] == "stale"
    assert result["result"] is not None
