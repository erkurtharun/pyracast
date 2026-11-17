from datetime import timedelta
from itertools import pairwise
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.test import Client

from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import ProductionObservation
from pyracast.plants.operations import ObservationInput, import_observations, revise_plant
from pyracast.plants.production import production_history
from pyracast.plants.production_changes import (
    preview_production,
)
from tests.support.plant_operations import make_plant
from tests.support.production_changes import edit, provider, write
from tests.support.provisional_measurements import DAY, acquisition

pytestmark = pytest.mark.django_db(transaction=True)


def test_native_shifted_hour_keeps_bounds_originals_and_missing_remainders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    acquisition((config, provider(system, plant)))
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    slot = start + timedelta(hours=10, minutes=30)
    saved = write(system, plant, [edit(slot, 7)])["ids"][0]
    result = production_history(plant, start_date=DAY, end_date=DAY, native_intervals=True)
    rows = result["intervals"]
    correction = next(row for row in rows if row["revision_id"] == saved)
    assert correction["starts_at"] == slot
    assert correction["ends_at"] == slot + timedelta(hours=1)
    assert correction["effective"]["energy_mwh"] == 7
    assert correction["can_edit"]
    assert correction["can_revert"]
    assert [row["starts_at"] for row in correction["provider_originals"]] == [
        start + timedelta(hours=10),
        start + timedelta(hours=11),
    ]
    gaps = [row for row in rows if row["effective"] is None]
    assert [(row["starts_at"], row["ends_at"]) for row in gaps] == [
        (start + timedelta(hours=10), slot),
        (slot + timedelta(hours=1), start + timedelta(hours=12)),
    ]
    assert all(row["can_edit"] for row in gaps)
    assert all(before["ends_at"] == after["starts_at"] for before, after in pairwise(rows))
    assert result["days"][0]["total_mwh"] is None
    write(
        system,
        plant,
        [edit(gaps[0]["starts_at"], 2, minutes=30), edit(gaps[1]["starts_at"], 3, minutes=30)],
    )
    filled = production_history(plant, start_date=DAY, end_date=DAY, native_intervals=True)
    assert filled["days"][0]["complete"]
    assert filled["days"][0]["total_mwh"] == 22 * 26 + 2 + 7 + 3
    assert (
        next(row for row in filled["intervals"] if row["revision_id"] == saved)["effective"][
            "energy_mwh"
        ]
        == 7
    )


def test_half_hours_are_native_and_hour_grid_only_sums_existing_manual_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    slot = start + timedelta(hours=10)
    write(
        system,
        plant,
        [
            edit(slot, 2, minutes=30),
            edit(slot + timedelta(minutes=30), 3, minutes=30, quality="outage"),
        ],
    )
    grid = production_history(plant, start_date=DAY, end_date=DAY)
    assert grid["intervals"][10]["effective"] == {
        "energy_mwh": 5,
        "quality": "mixed",
        "source": "multiple_manual",
        "source_kind": "manual",
        "provisional": False,
        "aggregated": True,
    }
    assert not grid["intervals"][10]["can_edit"]
    native = production_history(plant, start_date=DAY, end_date=DAY, native_intervals=True)
    values = [row for row in native["intervals"] if row["effective"] is not None]
    assert [row["effective"]["energy_mwh"] for row in values] == [2, 3]
    assert all(row["can_edit"] for row in values)
    assert all(not row["effective"]["aggregated"] for row in values)


def test_csv_warns_only_when_proposed_native_rows_leave_provider_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    acquisition((config, provider(system, plant)))
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    slot = start + timedelta(hours=10)

    def preview(observations: list[ObservationInput]) -> dict[str, Any]:
        return preview_production(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            configuration_id=config.pk,
            unit="MWh",
            observations=observations,
        )

    first = ObservationInput(slot, slot + timedelta(minutes=30), 1)
    second = ObservationInput(slot + timedelta(minutes=30), slot + timedelta(hours=1), 2)
    assert preview([first])["warnings"] == [
        {
            "code": "provider_hour_incomplete",
            "starts_at": slot,
            "ends_at": slot + timedelta(hours=1),
        }
    ]
    assert preview([first, second])["warnings"] == []
    following_hour = ObservationInput(slot + timedelta(hours=1), slot + timedelta(hours=2), 3)
    assert preview([first, second, following_hour])["warnings"] == []
    saved = write(system, plant, [edit(slot - timedelta(minutes=30), 3)])["ids"][0]
    # The existing shifted hour covers the start of this provider hour, even
    # though its native start precedes the preview's first input.
    complete = preview([second])
    assert complete["warnings"] == []
    correction = preview(
        [ObservationInput(slot - timedelta(minutes=30), slot + timedelta(minutes=30), 4)]
    )
    assert correction["changes"][0]["expected_revision_id"] == saved
    assert correction["preview"][0]["corrected"]


def test_native_previous_configuration_is_display_only_and_reentry_after_revert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    first = write(system, plant, [edit(start, 2)])["ids"][0]
    tombstone = write(system, plant, [edit(start, revision_id=first, action="revert")])["ids"][0]
    restored = write(system, plant, [edit(start, 3, revision_id=tombstone)])["ids"][0]
    assert ProductionObservation.objects.get(pk=restored).supersedes_id == tombstone
    revise_plant(
        system.member, system.tenant.pk, system.region.pk, plant.pk, name="New", dc_capacity_mw=150
    )
    plant.refresh_from_db()
    result = production_history(plant, start_date=DAY, end_date=DAY, native_intervals=True)
    assert result["intervals"][0]["observation"] is None
    assert result["intervals"][0]["effective"] is None
    assert result["intervals"][0]["edit_unavailable_reason"] == "configuration_mismatch"
    assert result["observations"][0]["id"] == restored


@pytest.mark.parametrize(
    ("unit", "changes", "message"),
    [
        ("W", "valid", "Use MWh"),
        ("MWh", "empty", "batch"),
        ("MWh", "action", "Unknown production action"),
        ("MWh", "revert", "no manual production"),
        ("MWh", "range", "within 31 local days"),
    ],
)
def test_invalid_batches_fail_at_application_boundary(
    monkeypatch: pytest.MonkeyPatch,
    unit: str,
    changes: str,
    message: str,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    rows = [edit(start)]
    if changes == "empty":
        rows = []
    elif changes == "action":
        rows = [edit(start, action="delete")]
    elif changes == "revert":
        rows = [edit(start, action="revert")]
    elif changes == "range":
        rows = [edit(start - timedelta(days=31)), edit(start)]
    with pytest.raises(ValidationError, match=message):
        write(system, plant, rows, unit=unit)
    assert not ProductionObservation.objects.exists()


def test_native_api_preview_roundtrip_and_scope_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    base = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/production/"
    )
    client = Client()
    client.force_login(system.member)
    config = plant.current_configuration_id
    preview = client.post(
        base + "preview/",
        {
            "configuration_id": config,
            "unit": "kWh",
            "observations": [
                {
                    "starts_at": start.isoformat(),
                    "ends_at": (start + timedelta(minutes=30)).isoformat(),
                    "energy": 1250,
                    "source": "csv",
                }
            ],
        },
        content_type="application/json",
    )
    assert preview.status_code == 200
    assert preview.json()["preview"][0]["new_energy_mwh"] == 1.25
    assert (
        client.post(
            base + "changes/",
            {"configuration_id": config, "unit": "kWh", "changes": preview.json()["changes"]},
            content_type="application/json",
        ).status_code
        == 200
    )
    native = client.get(
        base,
        {"start_date": DAY.isoformat(), "end_date": DAY.isoformat(), "native_intervals": "true"},
    )
    assert native.status_code == 200
    assert native.json()["native_intervals"]
    assert native.json()["intervals"][0]["effective"]["energy_mwh"] == 1.25
    missing_energy: dict[str, Any] = {
        "configuration_id": config,
        "unit": "MWh",
        "changes": [
            {
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
                "expected_revision_id": None,
                "action": "set",
            }
        ],
    }
    assert (
        client.post(base + "changes/", missing_energy, content_type="application/json").status_code
        == 400
    )
    missing_energy["configuration_id"] = 99999999
    missing_energy["changes"][0]["energy"] = 1
    assert (
        client.post(base + "changes/", missing_energy, content_type="application/json").status_code
        == 404
    )


def test_huge_finite_energy_does_not_publish_infinite_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    rows = [edit(start, 1e308, minutes=30), edit(start + timedelta(minutes=30), 1e308, minutes=30)]
    rows.extend(edit(start + timedelta(hours=hour), 1e308) for hour in range(1, 24))
    write(system, plant, rows)
    result = production_history(plant, start_date=DAY, end_date=DAY)
    assert result["days"][0]["complete"]
    assert result["days"][0]["total_mwh"] is None
    assert result["intervals"][0]["effective"] is None


def test_legacy_import_reads_only_live_head_and_rejects_withdrawal_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    original = write(system, plant, [edit(start, 2)])["ids"][0]
    replacement = write(system, plant, [edit(start, 3, revision_id=original)])["ids"][0]

    def legacy(energy: float) -> Any:
        return import_observations(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            configuration_id=plant.current_configuration_id or 0,
            unit="MWh",
            observations=[
                ObservationInput(start, start + timedelta(hours=1), energy, source="meter")
            ],
        )

    assert legacy(3)["ids"] == [replacement]
    with pytest.raises(ValidationError, match="Conflicting"):
        legacy(2)
    write(system, plant, [edit(start, revision_id=replacement, action="revert")])
    with pytest.raises(ValidationError, match="Conflicting"):
        legacy(3)
