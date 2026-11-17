from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant
from pyracast.errors import ConflictError
from pyracast.irradiance import local_day_bounds
from pyracast.plants.measurements import latest_measurement_day, select_measurements
from pyracast.plants.models import ProductionObservation
from pyracast.plants.operations import ObservationInput, revise_plant
from pyracast.plants.production import production_history
from pyracast.plants.production_changes import (
    ProductionChange,
    change_production,
    preview_production,
)
from tests.support.domain import NOW
from tests.support.errors import raises_code
from tests.support.plant_operations import make_plant
from tests.support.production_changes import edit, provider, write
from tests.support.provisional_measurements import DAY, acquisition

pytestmark = pytest.mark.django_db(transaction=True)


def test_correction_survives_imports_and_revert_uses_latest_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    source = (config, provider(system, plant))
    original = acquisition(source)
    start, end = local_day_bounds(DAY, system.binding.timezone)
    slot = start + timedelta(hours=10)
    first = write(system, plant, [edit(slot, 3500)], unit="kWh")["ids"][0]
    newer = acquisition(source, minute=1, values=["30"] * 24)
    result = production_history(plant, start_date=DAY, end_date=DAY)
    row = result["intervals"][10]
    assert row["effective"]["energy_mwh"] == 3.5
    assert row["effective"]["source_kind"] == "manual"
    assert row["provider_original"]["energy_mwh"] == 30
    assert row["provider_original"]["snapshot_id"] == newer.pk
    assert row["observation"]["recorded_by_id"] == system.member.pk
    assert row["can_edit"]
    assert row["can_revert"]
    assert not row["reference_available"]
    assert result["days"][0]["total_mwh"] == 23 * 30 + 3.5
    assert original.readings[10]["total_mwh"] == "26"
    second = write(system, plant, [edit(slot, 4, revision_id=first)])["ids"][0]
    assert ProductionObservation.objects.get(pk=first).energy_mwh == 3.5
    assert ProductionObservation.objects.get(pk=second).supersedes_id == first
    unchanged = write(system, plant, [edit(slot, 4, revision_id=second)])
    assert unchanged["created"] == 0
    assert unchanged["ids"] == [second]
    reverted = write(system, plant, [edit(slot, revision_id=second, action="revert")])["ids"][0]
    repeated = write(system, plant, [edit(slot, revision_id=reverted, action="revert")])
    assert repeated["created"] == 0
    assert repeated["ids"] == [reverted]
    rows, _ = select_measurements(config, start, end, NOW)
    assert rows[10].energy_mwh == 30
    assert rows[10].snapshot is not None
    assert rows[10].snapshot.pk == newer.pk
    result = production_history(plant, start_date=DAY, end_date=DAY)
    assert result["intervals"][10]["revision_id"] == reverted
    assert result["intervals"][10]["observation"] is None
    assert not result["intervals"][10]["can_revert"]
    assert len(result["revisions"]) == 3
    # Older fits retain the exact revision even when every write shares a timestamp.
    pinned, _ = select_measurements(config, start, end, NOW, observation_id_cutoff=first)
    assert pinned[10].energy_mwh == 3.5
    assert pinned[10].pk == first


def test_as_of_correction_and_revert_preserve_old_measurements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    start, end = local_day_bounds(DAY, system.binding.timezone)
    first = write(system, plant, [edit(start, 2)])["ids"][0]
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(minutes=1))
    second = write(system, plant, [edit(start, 3, revision_id=first)])["ids"][0]
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(minutes=2))
    write(system, plant, [edit(start, revision_id=second, action="revert")])
    assert select_measurements(config, start, end, NOW)[0][0].energy_mwh == 2
    assert select_measurements(config, start, end, NOW + timedelta(minutes=1))[0][0].energy_mwh == 3
    assert select_measurements(config, start, end, NOW + timedelta(minutes=2))[0] == []
    assert latest_measurement_day(config, end, NOW + timedelta(minutes=2), 1000) is None


def test_batch_rejects_stale_and_overlap_without_partial_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    first = write(system, plant, [edit(start, 2)])["ids"][0]
    with raises_code("production_revision_changed"):
        write(system, plant, [edit(start + timedelta(hours=1)), edit(start, 3)])
    assert ProductionObservation.objects.count() == 1
    with pytest.raises(ValidationError, match="overlapping"):
        write(system, plant, [edit(start, 3, revision_id=first), edit(start, minutes=30)])
    with pytest.raises(ValidationError, match="overlapping"):
        write(system, plant, [edit(start + timedelta(minutes=30), minutes=30)])
    assert ProductionObservation.objects.get(pk=first).is_current
    assert ProductionObservation.objects.count() == 1


@pytest.mark.parametrize("quality", ["invalid", "outage", "curtailed"])
def test_half_hour_manual_quality_suppresses_whole_provider_hour(
    monkeypatch: pytest.MonkeyPatch,
    quality: str,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    acquisition((config, provider(system, plant)))
    start, end = local_day_bounds(DAY, system.binding.timezone)
    slot = start + timedelta(hours=10)
    saved = write(system, plant, [edit(slot, 0, minutes=30, quality=quality)])["ids"][0]
    rows, _ = select_measurements(config, start, end, NOW)
    chosen = [
        row for row in rows if row.starts_at >= slot and row.starts_at < slot + timedelta(hours=1)
    ]
    assert len(chosen) == 1
    assert chosen[0].pk == saved
    assert chosen[0].quality == quality
    result = production_history(plant, start_date=DAY, end_date=DAY)
    assert result["intervals"][10]["edit_unavailable_reason"] == "observation_overlap"
    assert result["days"][0]["total_mwh"] is None
    half = production_history(plant, start_date=DAY, end_date=DAY, resolution_minutes=30)
    assert half["intervals"][20]["effective"]["energy_mwh"] == 0
    assert half["intervals"][21]["effective"] is None
    assert half["intervals"][20]["provider_original"]["ends_at"] == slot + timedelta(hours=1)


def test_preview_units_stale_token_and_missing_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    acquisition((config, provider(system, plant)), values=["0", None] + ["26"] * 22)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    result = preview_production(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=config.pk,
        unit="kWh",
        observations=[ObservationInput(start, start + timedelta(hours=1), 1500, source="csv")],
    )
    assert not ProductionObservation.objects.exists()
    assert result["changes"][0]["expected_revision_id"] is None
    assert result["preview"][0]["current_energy_mwh"] == 0
    assert result["preview"][0]["new_energy_mwh"] == 1.5
    write(system, plant, [edit(start, 2)])
    with raises_code("production_revision_changed"):
        write(system, plant, [ProductionChange(**result["changes"][0])], unit="kWh")
    history = production_history(plant, start_date=DAY, end_date=DAY)
    assert history["intervals"][1]["effective"] is None
    assert history["intervals"][1]["provider_original"] is None


def test_current_configuration_and_meter_basis_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    old_config = plant.current_configuration
    assert old_config is not None
    acquisition((old_config, provider(system, plant)))
    start, end = local_day_bounds(DAY, system.binding.timezone)
    first = write(system, plant, [edit(start, 2)])["ids"][0]
    revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name="New",
        dc_capacity_mw=130,
        measurement_basis="ac_generation",
    )
    plant.refresh_from_db()
    config = plant.current_configuration
    assert config is not None
    assert select_measurements(config, start, end, NOW)[0] == []
    history = production_history(plant, start_date=DAY, end_date=DAY)
    assert history["observations"][0]["id"] == first
    assert history["intervals"][0]["edit_unavailable_reason"] == "configuration_mismatch"
    assert history["intervals"][0]["provider_original"] is None
    with pytest.raises(ValidationError, match="previous configuration"):
        write(system, plant, [edit(start, 3, revision_id=first)])
    with pytest.raises(ValidationError, match="current approved configuration"):
        change_production(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            configuration_id=old_config.pk,
            unit="MWh",
            changes=[edit(start, 3, revision_id=first)],
        )


def test_api_permissions_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    base = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/production/"
    )
    client = Client()
    client.force_login(system.member)
    payload: dict[str, Any] = {
        "configuration_id": plant.current_configuration_id,
        "unit": "MWh",
        "changes": [
            {
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
                "expected_revision_id": None,
                "action": "set",
                "energy": 2,
            }
        ],
    }
    response = client.post(base + "changes/", payload, content_type="application/json")
    assert response.status_code == 200
    assert (
        client.post(base + "changes/", payload, content_type="application/json").status_code == 409
    )
    payload["changes"][0]["energy"] = True
    assert (
        client.post(base + "changes/", payload, content_type="application/json").status_code == 400
    )
    payload["changes"][0]["energy"] = 2
    payload["changes"] *= 2001
    assert (
        client.post(base + "changes/", payload, content_type="application/json").status_code == 400
    )
    reader = User.objects.create(username="reader")
    Membership.objects.create(user=reader, tenant=system.tenant)
    client.force_login(reader)
    readonly = client.get(base, {"start_date": DAY.isoformat(), "end_date": DAY.isoformat()})
    assert readonly.status_code == 200
    assert all(not row["can_edit"] for row in readonly.json()["intervals"])
    payload["changes"] = payload["changes"][:1]
    assert (
        client.post(base + "changes/", payload, content_type="application/json").status_code == 403
    )
    assert (
        client.post(
            base + "preview/",
            {
                "configuration_id": plant.current_configuration_id,
                "unit": "MWh",
                "observations": [
                    {
                        "starts_at": start.isoformat(),
                        "ends_at": (start + timedelta(hours=1)).isoformat(),
                        "energy": 1,
                        "source": "csv",
                    }
                ],
            },
            content_type="application/json",
        ).status_code
        == 403
    )
    stranger = User.objects.create(username="stranger")
    Membership.objects.create(
        user=stranger, tenant=Tenant.objects.create(name="Other"), role=Membership.Role.ADMIN
    )
    client.force_login(stranger)
    assert client.get(base).status_code == 403
    assert (
        client.post(base + "changes/", payload, content_type="application/json").status_code == 403
    )
    with pytest.raises(PermissionDenied):
        change_production(
            reader,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            configuration_id=plant.current_configuration_id or 0,
            unit="MWh",
            changes=[edit(start)],
        )


def test_concurrent_corrections_have_one_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    first = write(system, plant, [edit(start, 2)])["ids"][0]
    barrier = Barrier(2)

    def update(value: int) -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            write(system, plant, [edit(start, value, revision_id=first)])
            return "saved"
        except ConflictError as conflict:
            return conflict.code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update, value) for value in (3, 4)]
        assert sorted(future.result(timeout=20) for future in futures) == [
            "production_revision_changed",
            "saved",
        ]
    assert ProductionObservation.objects.count() == 2
    assert ProductionObservation.objects.filter(is_current=True).count() == 1


def test_database_current_overlap_and_revision_purge(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    first = write(system, plant, [edit(start, 2)])["ids"][0]
    write(system, plant, [edit(start, 3, revision_id=first)])
    with pytest.raises(IntegrityError), transaction.atomic():
        ProductionObservation.objects.create(
            plant=plant,
            configuration_id=plant.current_configuration_id,
            starts_at=start,
            ends_at=start + timedelta(minutes=30),
            energy_mwh=1,
            quality="valid",
            source="bad writer",
            recorded_by=system.member,
        )
    assert ProductionObservation.objects.filter(plant=plant).delete()[0] == 2
