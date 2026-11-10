from datetime import date, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client

from pyracast.forecasts.history import HISTORY_SELECTION_POLICY, select_actual_snapshot
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import PlantConfiguration, PlantForecast
from pyracast.regions.models import Region
from tests.test_domain import NOW, System
from tests.test_plant_operations import make_plant, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 8)


def actuals(system: System, *, acquired: datetime = NOW - timedelta(hours=1)) -> ForecastSnapshot:
    snapshot = snapshot_for(system, DAY, collected_at=acquired, source_kind="estimated_actuals")
    start, end = local_day_bounds(DAY, system.binding.timezone)
    snapshot.available_at = acquired
    snapshot.coverage_start, snapshot.coverage_end = start, end
    snapshot.save()
    snapshot.job.status = "succeeded"
    snapshot.job.save(update_fields=["status"])
    return snapshot


def test_past_actuals_use_declared_configuration_without_backdating_or_replacing_forecasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    reference = actuals(system)
    forecast_source = snapshot_for(system, DAY, power_kw=0.5)
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    archived = plant_outlook(*args, target_date=DAY, resolution_minutes=30)
    archived_payload = archived["result"]
    assert isinstance(archived_payload, dict)
    assert archived_payload["energy_mwh"] == 1560
    assert PlantForecast.objects.count() == 1
    PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(created_at=NOW)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    response = client.get(path, {"date": DAY.isoformat(), "data_source": "auto"})
    assert response.status_code == 200
    data = response.json()
    assert data["source_kind"] == "estimated_actuals"
    assert data["configuration_id"] == plant.current_configuration_id
    assert data["calibration_id"] is None
    assert data["freshness"] == "fresh"
    assert data["unavailable_reason"] is None
    assert data["result"]["energy_mwh"] == 1248
    assert len(data["result"]["intervals"]) == 48
    assert "historical_configuration_assumed" in data["result"]["quality_flags"]
    assert "estimated_actuals_not_metered" in data["result"]["quality_flags"]
    assert data["result"]["provenance"]["snapshot_id"] == reference.pk
    assert data["result"]["provenance"]["selection_policy"] == HISTORY_SELECTION_POLICY
    hourly = client.get(
        path,
        {"date": DAY.isoformat(), "data_source": "estimated_actuals", "resolution_minutes": "60"},
    )
    assert hourly.status_code == 200
    assert len(hourly.json()["result"]["intervals"]) == 24
    assert hourly.json()["result"]["energy_mwh"] == 1248
    assert PlantForecast.objects.get(snapshot=forecast_source).payload == archived_payload
    assert PlantForecast.objects.count() == 1
    assert PlantConfiguration.objects.get(pk=plant.current_configuration_id).created_at == NOW
    client.force_login(User.objects.create_user("unrelated-user"))
    assert client.get(path, {"date": DAY.isoformat(), "data_source": "auto"}).status_code == 403


@pytest.mark.parametrize(
    "changes",
    [
        {"collected_at": NOW + timedelta(seconds=1)},
        {"available_at": NOW + timedelta(seconds=1)},
        {"issued_at": NOW + timedelta(seconds=1)},
        {"collected_at": datetime(2026, 11, 7, tzinfo=NOW.tzinfo)},
        {"coverage_start": NOW},
        {"coverage_end": datetime(2026, 11, 7, tzinfo=NOW.tzinfo)},
        {"source_kind": "forecast"},
        {"available_at": None},
    ],
)
def test_history_selection_excludes_unavailable_or_unmatched_revisions(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    system, _ = make_plant(monkeypatch)
    original = actuals(system, acquired=NOW - timedelta(hours=2))
    changed = actuals(system)
    ForecastSnapshot.objects.filter(pk=changed.pk).update(**changes)
    selected = select_actual_snapshot(system.binding, DAY, NOW)
    assert selected is not None
    assert selected.pk == original.pk


def test_history_selection_keeps_acquisition_and_publication_cutoffs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    first = actuals(system, acquired=NOW - timedelta(hours=2))
    second = actuals(system)
    second.available_at = NOW
    second.issued_at = NOW - timedelta(hours=1)
    second.save()
    assert select_actual_snapshot(system.binding, DAY, NOW) == second
    assert select_actual_snapshot(system.binding, DAY, NOW - timedelta(minutes=1)) == first
    second.job.status = "stale"
    second.job.save()
    assert select_actual_snapshot(system.binding, DAY, NOW) == first
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    data = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=30,
        source="estimated_actuals",
    )
    assert data["freshness"] == "stale"


def test_missing_history_falls_back_only_when_automatic(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    snapshot_for(system, DAY)
    automatic = plant_outlook(
        *args, target_date=DAY, resolution_minutes=30, source="auto", as_of=NOW
    )
    assert automatic["source_kind"] == "forecast"
    assert automatic["result"] is not None
    missing = plant_outlook(
        *args, target_date=DAY, resolution_minutes=30, source="estimated_actuals"
    )
    assert missing["result"] is None
    assert missing["unavailable_reason"] == "estimated_actuals_unavailable"
    assert missing["freshness"] == "missing"
    PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(created_at=NOW)
    missing_config = plant_outlook(
        *args,
        target_date=DAY,
        resolution_minutes=60,
        source="estimated_actuals",
        as_of=NOW - timedelta(seconds=1),
    )
    assert missing_config["configuration_id"] is None
    assert missing_config["unavailable_reason"] == "configuration_unavailable"
    future = plant_outlook(
        *args, target_date=NOW.date() + timedelta(days=1), resolution_minutes=30, source="auto"
    )
    assert future["source_kind"] == "forecast"


@pytest.mark.parametrize(
    "values",
    [
        {"source": "unknown"},
        {"resolution_minutes": True},
        {"resolution_minutes": 15},
        {"target_date": NOW.date()},
        {"target_date": NOW.date() - timedelta(days=367)},
        {"as_of": NOW.replace(tzinfo=None)},
        {"as_of": NOW + timedelta(seconds=1)},
    ],
)
def test_history_rejects_incomplete_dates_and_invalid_arguments(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]
) -> None:
    system, plant = make_plant(monkeypatch)
    arguments: dict[str, Any] = {
        "target_date": DAY,
        "resolution_minutes": 30,
        "source": "estimated_actuals",
    } | values
    with pytest.raises(ValidationError):
        plant_outlook(system.member, system.tenant.pk, system.region.pk, plant.pk, **arguments)


def test_corrupt_history_cannot_return_a_fabricated_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot = actuals(system)
    snapshot.normalized_intervals[0]["power_kw"] = -1
    snapshot.save()
    with pytest.raises(ValidationError, match="calculation_out_of_range"):
        plant_outlook(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            target_date=DAY,
            resolution_minutes=30,
            source="estimated_actuals",
        )
