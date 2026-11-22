import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier
from typing import Any

import pytest
from django.db import close_old_connections, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext

from pyracast.accounts.models import Tenant
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import (
    Plant,
    PlantEvent,
    PlantEventAction,
    ProductionObservation,
)
from pyracast.plants.operations import revise_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.plants.production import production_history
from pyracast.regions.models import Region
from tests.support.concurrency import row_locked_elsewhere, short_lock_timeout
from tests.support.domain import System
from tests.support.errors import raises_code
from tests.support.plant_operations import make_plant
from tests.support.production_changes import edit, write
from tests.support.provisional_measurements import DAY

pytestmark = pytest.mark.django_db(transaction=True)


def production_url(system: System, plant: Plant) -> str:
    return (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/production/"
    )


def admin(system: System) -> Client:
    client = Client()
    client.force_login(system.member)
    return client


def change(start: datetime, energy: float, revision: int | None, **extra: Any) -> dict[str, Any]:
    return {
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=1)).isoformat(),
        "expected_revision_id": revision,
        "action": "set",
        "energy": energy,
        "source": "meter",
        **extra,
    }


def events(plant: Plant) -> list[dict[str, Any]]:
    return list(
        PlantEvent.objects.filter(plant=plant, action=PlantEventAction.PRODUCTION_CHANGED)
        .order_by("pk")
        .values_list("details", flat=True)
    )


def test_replayed_production_change_answers_exactly_as_the_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    client = admin(system)
    url = production_url(system, plant) + "changes/"
    payload = {
        "configuration_id": plant.current_configuration_id,
        "unit": "MWh",
        "changes": [change(start, 2, None)],
    }
    first = client.post(url, payload, content_type="application/json")
    replay = client.post(url, payload, content_type="application/json")
    assert first.status_code == replay.status_code == 200, replay.content
    assert replay.json() == first.json()
    assert first.json()["created"] == 1
    head = first.json()["ids"][0]
    revert = {
        "configuration_id": plant.current_configuration_id,
        "unit": "MWh",
        "changes": [
            {
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
                "expected_revision_id": head,
                "action": "revert",
            }
        ],
    }
    reverted = client.post(url, revert, content_type="application/json")
    replayed_revert = client.post(url, revert, content_type="application/json")
    assert reverted.status_code == replayed_revert.status_code == 200, replayed_revert.content
    assert replayed_revert.json() == reverted.json()
    assert ProductionObservation.objects.filter(plant=plant).count() == 2
    assert len(events(plant)) == 2
    different = client.post(
        url,
        payload | {"changes": [change(start, 3, None)]},
        content_type="application/json",
    )
    assert different.status_code == 409
    assert different.json()["code"] == "production_revision_changed"


def test_batch_mixing_an_applied_change_with_a_new_one_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    write(system, plant, [edit(start, 2)])
    with raises_code("production_revision_changed"):
        write(system, plant, [edit(start, 2), edit(start + timedelta(hours=1), 5)])
    assert ProductionObservation.objects.filter(plant=plant).count() == 1
    assert len(events(plant)) == 1


def test_concurrent_identical_replays_store_one_revision_and_answer_alike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    first = write(system, plant, [edit(start, 2)])["ids"][0]
    barrier = Barrier(2)

    def replay() -> Any:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return write(system, plant, [edit(start, 3, revision_id=first)])
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = [future.result(timeout=20) for future in [pool.submit(replay) for _ in "ab"]]
    assert answers[0] == answers[1]
    assert answers[0]["created"] == 1
    assert ProductionObservation.objects.filter(plant=plant).count() == 2
    assert len(events(plant)) == 2


@pytest.mark.parametrize("model", [Tenant, Region, Plant])
def test_production_preview_does_not_wait_for_row_locks_held_elsewhere(
    monkeypatch: pytest.MonkeyPatch, model: type[Tenant] | type[Region] | type[Plant]
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    client = admin(system)
    pk = {Tenant: system.tenant.pk, Region: system.region.pk, Plant: plant.pk}[model]
    body = {
        "configuration_id": plant.current_configuration_id,
        "unit": "MWh",
        "observations": [
            {
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
                "energy": 2,
                "source": "csv",
            }
        ],
    }
    with row_locked_elsewhere(model, pk), short_lock_timeout():
        response = client.post(
            production_url(system, plant) + "preview/", body, content_type="application/json"
        )
    assert response.status_code == 200, response.content
    assert response.json()["changes"][0]["expected_revision_id"] is None


def test_month_of_half_hours_commits_with_a_bounded_statement_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    first, _ = local_day_bounds(date(2026, 10, 8), system.binding.timezone)
    _, last = local_day_bounds(date(2026, 11, 7), system.binding.timezone)
    rows = []
    cursor = first
    while cursor < last:
        rows.append(edit(cursor, 0.5, minutes=30))
        cursor += timedelta(minutes=30)
    assert len(rows) == 1488
    with transaction.atomic(), CaptureQueriesContext(connection) as queries:
        result = write(system, plant, rows)
    assert result["created"] == 1488
    assert len(queries) <= 30
    assert ProductionObservation.objects.filter(plant=plant, is_current=True).count() == 1488


def test_production_change_event_records_origin_counts_and_file_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    kept = write(system, plant, [edit(start, 2)])["ids"][0]
    client = admin(system)
    response = client.post(
        production_url(system, plant) + "changes/",
        {
            "configuration_id": plant.current_configuration_id,
            "unit": "MWh",
            "origin": "csv_import",
            "file_name": "meter.csv",
            "file_sha256": "a" * 64,
            "changes": [
                change(start, 2, kept),
                change(start + timedelta(hours=1), 4, None),
            ],
        },
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    created = response.json()["ids"][1]
    assert events(plant)[-1] == {
        "origin": "csv_import",
        "configuration_id": plant.current_configuration_id,
        "created_ids": [created],
        "unchanged": 1,
        "file_name": "meter.csv",
        "file_sha256": "a" * 64,
    }
    assert events(plant)[0]["origin"] == "editor"


def test_energy_above_plant_capacity_needs_confirmation_and_is_previewed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    client = admin(system)
    base = production_url(system, plant)
    preview = client.post(
        base + "preview/",
        {
            "configuration_id": plant.current_configuration_id,
            "unit": "MWh",
            "observations": [
                {
                    "starts_at": start.isoformat(),
                    "ends_at": (start + timedelta(hours=1)).isoformat(),
                    "energy": 4200,
                    "source": "csv",
                },
                {
                    "starts_at": (start + timedelta(hours=1)).isoformat(),
                    "ends_at": (start + timedelta(hours=2)).isoformat(),
                    "energy": 110,
                    "source": "csv",
                },
            ],
        },
        content_type="application/json",
    )
    assert preview.status_code == 200, preview.content
    assert preview.json()["warnings"] == [
        {
            "code": "energy_exceeds_capacity",
            "starts_at": start.isoformat().replace("+00:00", "Z"),
            "ends_at": (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        }
    ]
    payload = {
        "configuration_id": plant.current_configuration_id,
        "unit": "MWh",
        "changes": [change(start, 4200, None)],
    }
    refused = client.post(base + "changes/", payload, content_type="application/json")
    assert refused.status_code == 400, refused.content
    assert refused.json()["code"] == "production_implausible"
    assert not ProductionObservation.objects.exists()
    confirmed = client.post(
        base + "changes/", payload | {"confirm_implausible": True}, content_type="application/json"
    )
    assert confirmed.status_code == 200, confirmed.content
    plausible = write(system, plant, [edit(start + timedelta(hours=1), 110)])
    assert plausible["created"] == 1
    with raises_code("production_implausible"):
        write(system, plant, [edit(start + timedelta(hours=2), 63, minutes=30)])


def test_earlier_configuration_correction_stays_visible_without_entering_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    declared = plant.current_configuration
    assert declared is not None
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    saved = write(system, plant, [edit(start, 2)])["ids"][0]
    revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        PlantParameters(name="New", dc_capacity_mw=150),
    )
    plant.refresh_from_db()
    for native in (True, False):
        result = production_history(plant, start_date=DAY, end_date=DAY, native_intervals=native)
        row = result["intervals"][0]
        assert row["effective"] is None
        assert row["edit_unavailable_reason"] == "configuration_mismatch"
        [earlier] = row["previous_configuration_observations"]
        assert earlier["id"] == saved
        assert earlier["configuration_id"] == declared.pk
        assert earlier["configuration_revision"] == declared.revision
        assert earlier["energy_mwh"] == 2
        assert result["days"][0]["total_mwh"] is None
    client = admin(system)
    response = client.get(
        production_url(system, plant),
        {"start_date": DAY.isoformat(), "end_date": DAY.isoformat(), "native_intervals": "true"},
    )
    [earlier] = response.json()["intervals"][0]["previous_configuration_observations"]
    assert earlier["id"] == saved


def test_production_history_payload_holds_only_declared_fields_within_a_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    write(system, plant, [edit(start, 2)])
    client = admin(system)
    response = client.get(
        production_url(system, plant),
        {
            "start_date": (DAY - timedelta(days=30)).isoformat(),
            "end_date": DAY.isoformat(),
            "resolution_minutes": "30",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert {"observations", "revisions"}.isdisjoint(body)
    assert len(body["intervals"]) == 31 * 48
    assert {
        "reference_available",
        "can_enter",
        "unavailable_reason",
        "provider_original",
    }.isdisjoint(body["intervals"][0])
    assert len(json.dumps(body)) <= 1_100_000
