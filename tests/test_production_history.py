import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.actuals import select_archived_actual_source
from pyracast.forecasts.faults import DamagedStoredDataError
from pyracast.irradiance import local_day_bounds
from pyracast.plants.availability import (
    actual_power_intervals,
    available_date_bounds,
    interval_is_covered,
    reference_intervals,
)
from pyracast.plants.models import Plant, ProductionObservation
from pyracast.plants.numerics import MODEL_VERSION, PowerInterval
from pyracast.plants.operations import revise_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.plants.production import production_history
from pyracast.plants.production_changes import ObservationInput, import_observations
from pyracast.regions.models import Region
from tests.support.domain import NOW, System
from tests.support.errors import raises_code
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant, saved_actuals, snapshot_for
from tests.support.production_history import upload

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 8)


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
    assert sum(day["available_count"] for day in result["days"]) == 168
    older = DAY - timedelta(days=9)
    days = production_history(plant, start_date=older, end_date=DAY)["days"]
    assert sum(day["available_count"] for day in days) == 168
    valid, _ = local_day_bounds(DAY, system.binding.timezone)
    invalid, _ = local_day_bounds(older, system.binding.timezone)
    with raises_code("estimated_actuals_unavailable"):
        upload(
            system,
            plant,
            [
                ObservationInput(valid, valid + timedelta(hours=1), 1),
                ObservationInput(invalid, invalid + timedelta(hours=1), 1),
            ],
        )
    assert not ProductionObservation.objects.exists()


# A point in the open Pacific, kept in Lima's zone: Astral finds no sunset for the UTC date
# 2026-11-04 because the sunsets around it fall just before and after 00:00 UTC.
ASTRAL_MISS_DAY = date(2026, 11, 4)


def pacific_plant(monkeypatch: pytest.MonkeyPatch) -> tuple[System, Plant]:
    system, plant = make_plant(monkeypatch)
    location = {"latitude": -20.0, "longitude": -87.5, "timezone": "America/Lima"}
    SiteBinding.objects.filter(pk=system.binding.pk).update(**location)
    Region.objects.filter(pk=system.region.pk).update(**location)
    system.binding.refresh_from_db()
    plant.refresh_from_db()
    saved_actuals(system, ASTRAL_MISS_DAY)
    return system, plant


def test_history_reads_where_astral_misses_an_event(monkeypatch: pytest.MonkeyPatch) -> None:
    _, plant = pacific_plant(monkeypatch)
    result = production_history(plant, start_date=date(2026, 11, 2), end_date=DAY)
    assert len(result["intervals"]) == 7 * 24
    assert sum(day["available_count"] for day in result["days"]) == 24


def test_history_saves_where_astral_misses_an_event(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = pacific_plant(monkeypatch)
    start, _ = local_day_bounds(ASTRAL_MISS_DAY, "America/Lima")
    noon = start + timedelta(hours=12)
    assert upload(system, plant, [ObservationInput(noon, noon + timedelta(hours=1), 1)]) == {
        "created": 1,
        "existing": 0,
        "count": 1,
        "ids": [ProductionObservation.objects.get().pk],
    }


@pytest.mark.parametrize("power", [None, -1])
def test_reference_holes_disable_only_affected_slots(
    monkeypatch: pytest.MonkeyPatch, power: Any
) -> None:
    system, plant = make_plant(monkeypatch)
    source = saved_actuals(system, DAY)
    source.normalized_intervals[1]["power_kw"] = power
    stored_again(source)
    result = production_history(plant, start_date=DAY, end_date=DAY)
    assert result["days"][0]["available_count"] == 23
    half = production_history(plant, start_date=DAY, end_date=DAY, resolution_minutes=30)
    assert half["days"][0]["available_count"] == 47
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    with raises_code("estimated_actuals_unavailable"):
        upload(system, plant, [ObservationInput(start, start + timedelta(hours=1), 0)])


@pytest.mark.parametrize("power", [True, "bad", 10**400], ids=["boolean", "text", "beyond-a-float"])
def test_a_reference_power_that_is_not_a_number_refuses_the_production_read(
    monkeypatch: pytest.MonkeyPatch, power: Any
) -> None:
    system, plant = make_plant(monkeypatch)
    source = saved_actuals(system, DAY)
    source.normalized_intervals[1]["power_kw"] = power
    stored_again(source)
    with raises_code("calculation_out_of_range"):
        production_history(plant, start_date=DAY, end_date=DAY)


def test_partial_oldest_day_and_missing_interval_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = saved_actuals(system, DAY)
    source.normalized_intervals = source.normalized_intervals[1:]
    stored_again(source)
    days = production_history(plant, start_date=DAY, end_date=DAY)["days"]
    assert days[0]["available_count"] == 23


def test_mixed_resolution_preserves_observations_after_revision_and_reference_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = saved_actuals(system, DAY)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    row = ObservationInput(start, start + timedelta(minutes=30), 0)
    assert upload(system, plant, [row])["created"] == 1
    source.source_kind = "forecast"
    stored_again(source)
    assert upload(system, plant, [row])["existing"] == 1
    original_id = plant.current_configuration_id
    revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        PlantParameters(name="New", dc_capacity_mw=140),
    )
    plant.refresh_from_db()
    saved = ProductionObservation.objects.get().pk
    result = production_history(plant, start_date=DAY, end_date=DAY)
    assert result["intervals"][0]["edit_unavailable_reason"] == "observation_overlap"
    assert [row["id"] for row in result["intervals"][0]["previous_configuration_observations"]] == [
        saved
    ]
    half = production_history(plant, start_date=DAY, end_date=DAY, resolution_minutes=30)
    assert half["intervals"][0]["observation"]["energy_mwh"] == 0
    [earlier] = half["intervals"][0]["previous_configuration_observations"]
    assert earlier["configuration_id"] == original_id
    assert half["intervals"][1]["previous_configuration_observations"] == []
    assert half["intervals"][0]["edit_unavailable_reason"] == "configuration_mismatch"
    native = production_history(plant, start_date=DAY, end_date=DAY, native_intervals=True)
    first = native["intervals"][0]
    assert first["ends_at"] - first["starts_at"] == timedelta(hours=1)
    assert first["edit_unavailable_reason"] == "configuration_mismatch"
    assert [row["id"] for row in first["previous_configuration_observations"]] == [saved]
    assert original_id is not None
    with pytest.raises(ValidationError, match="changed after these values were entered"):
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
    assert not any(day["available_count"] for day in result["days"])
    source = saved_actuals(system, DAY)
    source.available_at = NOW + timedelta(seconds=1)
    stored_again(source)
    result = production_history(plant, start_date=DAY, end_date=DAY)
    assert result["days"][0]["available_count"] == 0
    source.available_at = NOW
    stored_again(source)
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    result = production_history(plant, start_date=DAY, end_date=DAY)
    assert all(
        row["edit_unavailable_reason"] == "configuration_not_current" for row in result["intervals"]
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
    assert result["days"][0]["available_count"] == count
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


def test_a_damaged_saved_day_refuses_the_production_read_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    system, plant = make_plant(monkeypatch)
    source = saved_actuals(system, DAY)
    rows = [dict(row) for row in source.normalized_intervals]
    rows[3]["start"] = "08.11.2026 03:00"
    stored_again(source, normalized_intervals=rows)
    client = Client()
    client.force_login(system.member)
    url = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/production/"
    )
    with caplog.at_level(logging.WARNING, logger="pyracast.forecasts.faults"):
        response = client.get(url, {"start_date": DAY.isoformat(), "end_date": DAY.isoformat()})
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "calculation_out_of_range"
    [warning] = [r.getMessage() for r in caplog.records if r.name == "pyracast.forecasts.faults"]
    assert (
        f"plant {plant.pk} day={DAY.isoformat()} step=production "
        f"record=forecast_snapshot:{source.pk} error=ValueError"
    ) in warning
    assert "08.11.2026" not in warning


def test_configless_history_and_source_interval_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    plant.current_configuration = None
    plant.save()
    result = production_history(plant)
    assert result["configuration_id"] is None
    assert all(
        row["edit_unavailable_reason"] == "configuration_unavailable" for row in result["intervals"]
    )
    start = datetime(2026, 11, 8, tzinfo=UTC)
    assert not interval_is_covered(
        (PowerInterval(start, start + timedelta(hours=1), None),), start, start + timedelta(hours=1)
    )
    configuration = plant.configurations.get()
    assert reference_intervals(configuration, DAY, NOW) == ()
    source = saved_actuals(system, DAY)
    source.normalized_intervals[0]["start"] = "2026-11-07T21:00:00"
    stored_again(source)
    with pytest.raises(DamagedStoredDataError) as damage:
        reference_intervals(configuration, DAY, NOW)
    assert damage.value.identity == source.pk


@pytest.mark.parametrize("invalid_bounds", ["naive_start", "naive_end", "zero", "reversed"])
def test_invalid_actual_bounds_refuse_the_snapshot_instead_of_an_older_one(
    monkeypatch: pytest.MonkeyPatch, invalid_bounds: str
) -> None:
    system, plant = make_plant(monkeypatch)
    saved_actuals(system, DAY)
    latest = saved_actuals(system, DAY)
    first = latest.normalized_intervals[0]
    original_start = first["start"]
    original_end = first["end"]
    if invalid_bounds == "naive_start":
        first["start"] = datetime.fromisoformat(original_start).replace(tzinfo=None).isoformat()
    elif invalid_bounds == "naive_end":
        first["end"] = datetime.fromisoformat(original_end).replace(tzinfo=None).isoformat()
    elif invalid_bounds == "zero":
        first["end"] = original_start
    else:
        first["end"] = (datetime.fromisoformat(original_start) - timedelta(minutes=30)).isoformat()
    stored_again(latest)
    for read in (
        lambda: select_archived_actual_source(system.binding, DAY, NOW),
        lambda: reference_intervals(plant.configurations.get(), DAY, NOW),
    ):
        with pytest.raises(DamagedStoredDataError) as damage:
            read()
        assert (damage.value.kind, damage.value.identity) == ("forecast_snapshot", latest.pk)
    with raises_code("calculation_out_of_range"):
        production_history(plant, start_date=DAY, end_date=DAY)
    latest.refresh_from_db()
    assert latest.normalized_intervals[0] == first


@pytest.mark.parametrize("power", [None, float("nan"), -1.0])
def test_incomplete_or_invalid_power_never_covers_interval(power: float | None) -> None:
    start = datetime(2026, 11, 8, tzinfo=UTC)
    end = start + timedelta(hours=1)
    assert not interval_is_covered((PowerInterval(start, end, power),), start, end)


@pytest.mark.parametrize(
    ("case", "fault"),
    [("nonfinite", "ValueError"), ("naive_end", "ValueError"), ("reversed", "ValueError")],
)
def test_invalid_source_intervals_are_damage_of_their_snapshot(
    monkeypatch: pytest.MonkeyPatch, case: str, fault: str
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
    with pytest.raises(DamagedStoredDataError) as damage:
        actual_power_intervals(source, configuration, model_version=MODEL_VERSION)
    assert (damage.value.identity, damage.value.fault) == (source.pk, fault)


def test_current_incomplete_day_does_not_become_available(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.configurations.get()
    source = saved_actuals(system, NOW.date())
    source.collected_at = source.available_at = NOW
    stored_again(source)
    assert available_date_bounds(configuration, NOW) == (None, None)
