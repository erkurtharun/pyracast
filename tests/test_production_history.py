from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant
from pyracast.irradiance import local_day_bounds
from pyracast.plants import availability
from pyracast.plants.availability import (
    available_date_bounds,
    interval_is_covered,
    reference_intervals,
)
from pyracast.plants.models import Plant, ProductionObservation
from pyracast.plants.numerics import PowerInterval
from pyracast.plants.operations import (
    ObservationImportResult,
    ObservationInput,
    import_observations,
    revise_plant,
)
from pyracast.plants.production import production_history
from pyracast.regions.models import Region
from tests.test_domain import NOW, System
from tests.test_plant_operations import make_plant, saved_actuals, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 8)


def upload(system: System, plant: Plant, rows: list[ObservationInput]) -> ObservationImportResult:
    assert plant.current_configuration_id is not None
    return import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="MWh",
        observations=rows,
    )


def test_seven_saved_days_do_not_enable_ten_and_batch_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    for offset in range(7):
        saved_actuals(system, DAY - timedelta(days=offset))
    result = production_history(plant)
    assert result["start_date"] == date(2026, 11, 2)
    assert result["end_date"] == DAY
    assert len(result["intervals"]) == 168
    assert all(row["can_enter"] for row in result["intervals"])
    older = DAY - timedelta(days=9)
    rows = production_history(plant, start_date=older, end_date=DAY)["intervals"]
    assert sum(row["can_enter"] for row in rows) == 168
    valid, _ = local_day_bounds(DAY, system.binding.timezone)
    invalid, _ = local_day_bounds(older, system.binding.timezone)
    with pytest.raises(ValidationError, match="estimated_actuals_unavailable"):
        upload(
            system,
            plant,
            [
                ObservationInput(valid, valid + timedelta(hours=1), 1),
                ObservationInput(invalid, invalid + timedelta(hours=1), 1),
            ],
        )
    assert not ProductionObservation.objects.exists()


@pytest.mark.parametrize("power", [None, -1, True, "bad"])
def test_reference_holes_disable_only_affected_slots(
    monkeypatch: pytest.MonkeyPatch, power: Any
) -> None:
    system, plant = make_plant(monkeypatch)
    source = saved_actuals(system, DAY)
    source.normalized_intervals[1]["power_kw"] = power
    source.save()
    result = production_history(plant, start_date=DAY, end_date=DAY)
    assert not result["intervals"][0]["can_enter"]
    assert result["intervals"][1]["can_enter"]
    half = production_history(plant, start_date=DAY, end_date=DAY, resolution_minutes=30)
    assert half["intervals"][0]["can_enter"]
    assert not half["intervals"][1]["can_enter"]
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    with pytest.raises(ValidationError, match="estimated_actuals_unavailable"):
        upload(system, plant, [ObservationInput(start, start + timedelta(hours=1), 0)])


def test_partial_oldest_day_and_missing_interval_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = saved_actuals(system, DAY)
    source.normalized_intervals = source.normalized_intervals[1:]
    source.save()
    rows = production_history(plant, start_date=DAY, end_date=DAY)["intervals"]
    assert not rows[0]["can_enter"]
    assert rows[1]["can_enter"]


def test_mixed_resolution_preserves_observations_after_revision_and_reference_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = saved_actuals(system, DAY)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    row = ObservationInput(start, start + timedelta(minutes=30), 0)
    assert upload(system, plant, [row])["created"] == 1
    source.source_kind = "forecast"
    source.save()
    assert upload(system, plant, [row])["existing"] == 1
    original_id = plant.current_configuration_id
    revise_plant(
        system.member, system.tenant.pk, system.region.pk, plant.pk, name="New", dc_capacity_mw=140
    )
    plant.refresh_from_db()
    result = production_history(plant, start_date=DAY, end_date=DAY)
    assert result["intervals"][0]["unavailable_reason"] == "observation_overlap"
    assert result["observations"][0]["configuration_id"] == original_id
    half = production_history(plant, start_date=DAY, end_date=DAY, resolution_minutes=30)
    assert half["intervals"][0]["observation"]["energy_mwh"] == 0
    assert half["intervals"][0]["unavailable_reason"] == "already_recorded"
    assert original_id is not None
    with pytest.raises(ValidationError, match="current approved configuration"):
        import_observations(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            configuration_id=original_id,
            unit="MWh",
            observations=[
                ObservationInput(start + timedelta(hours=1), start + timedelta(hours=2), 1)
            ],
        )


def test_forecasts_future_publication_and_wrong_binding_never_enable_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot_for(system, DAY)
    result = production_history(plant)
    assert result["available_start_date"] is None
    assert not any(row["can_enter"] for row in result["intervals"])
    source = saved_actuals(system, DAY)
    source.available_at = NOW + timedelta(seconds=1)
    source.save()
    result = production_history(plant, start_date=DAY, end_date=DAY)
    assert not any(row["can_enter"] for row in result["intervals"])
    source.available_at = NOW
    source.save()
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    result = production_history(plant, start_date=DAY, end_date=DAY)
    assert all(
        row["unavailable_reason"] == "configuration_not_current" for row in result["intervals"]
    )


@pytest.mark.parametrize(("day", "count"), [(date(2026, 3, 29), 46), (date(2026, 10, 25), 50)])
def test_history_preserves_dst_interval_bounds(
    monkeypatch: pytest.MonkeyPatch, day: date, count: int
) -> None:
    system, plant = make_plant(monkeypatch)
    system.binding.timezone = "Europe/Berlin"
    system.binding.save(update_fields=("timezone",))
    saved_actuals(system, day)
    result = production_history(plant, start_date=day, end_date=day, resolution_minutes=30)
    assert len(result["intervals"]) == count
    assert all(row["can_enter"] for row in result["intervals"]), {
        (row["reference_available"], row["unavailable_reason"]) for row in result["intervals"]
    }
    assert len({row["starts_at"] for row in result["intervals"]}) == count


def test_history_api_validates_bounds_and_tenant_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    client = Client()
    client.force_login(system.member)
    url = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/production/"
    )
    response = client.get(url)
    assert response.status_code == 200
    assert response.json()["available_start_date"] is None
    for query in (
        {"start_date": "2026-11-01"},
        {"end_date": "2026-11-01"},
        {"start_date": "2026-11-08", "end_date": "2026-11-07"},
        {"start_date": "2026-10-01", "end_date": "2026-11-08"},
        {"resolution_minutes": "15"},
    ):
        assert client.get(url, query).status_code == 400
    with pytest.raises(ValidationError):
        production_history(plant, resolution_minutes=15)
    other = Tenant.objects.create(name="Other")
    user = User.objects.create_user(username="other")
    Membership.objects.create(tenant=other, user=user)
    client.force_login(user)
    assert client.get(url).status_code == 403
    assert (
        client.get(url.replace(f"tenants/{system.tenant.pk}", f"tenants/{other.pk}")).status_code
        == 404
    )


def test_configless_history_and_source_interval_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    plant.current_configuration = None
    plant.save()
    result = production_history(plant)
    assert result["configuration_id"] is None
    assert all(
        row["unavailable_reason"] == "configuration_unavailable" for row in result["intervals"]
    )
    start = datetime(2026, 11, 8, tzinfo=UTC)
    assert not interval_is_covered(
        (PowerInterval(start, start + timedelta(hours=1), None),), start, start + timedelta(hours=1)
    )
    configuration = plant.configurations.get()
    assert reference_intervals(configuration, DAY, NOW) == ()
    source = saved_actuals(system, DAY)
    source.normalized_intervals[0]["start"] = "2026-11-07T21:00:00"
    source.save()
    assert len(reference_intervals(configuration, DAY, NOW)) == 47
    monkeypatch.setattr(timezone, "now", lambda: NOW)


@pytest.mark.parametrize("power", [None, float("nan"), -1.0])
def test_incomplete_or_invalid_power_never_covers_interval(power: float | None) -> None:
    start = datetime(2026, 11, 8, tzinfo=UTC)
    end = start + timedelta(hours=1)
    assert not interval_is_covered((PowerInterval(start, end, power),), start, end)


@pytest.mark.parametrize("case", ["nonfinite", "naive_end", "reversed"])
def test_invalid_source_intervals_remain_missing(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.configurations.get()
    source = saved_actuals(system, DAY)
    if case == "nonfinite":
        source.normalized_intervals[0]["power_kw"] = float("inf")
    elif case == "naive_end":
        source.normalized_intervals[0]["end"] = "2026-11-07T21:30:00"
    else:
        source.normalized_intervals[0]["end"] = source.normalized_intervals[0]["start"]
    monkeypatch.setattr(availability, "select_actual_snapshot", lambda *args: source)
    assert len(reference_intervals(configuration, DAY, NOW)) == 47


def test_current_incomplete_day_does_not_become_available(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.configurations.get()
    source = saved_actuals(system, NOW.date())
    source.collected_at = source.available_at = NOW
    source.save()
    assert available_date_bounds(configuration, NOW) == (None, None)
