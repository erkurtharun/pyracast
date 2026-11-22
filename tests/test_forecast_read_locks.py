from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, timedelta
from threading import Event
from typing import Any, cast

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.models import Model
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from pyracast.accounts.models import Membership
from pyracast.connections.models import ProviderAccount
from pyracast.incidents.operations import save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.plants.activation import activate_calibration
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import Plant
from pyracast.plants.operations import revise_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.models import Region
from tests.support.concurrency import row_locked_elsewhere, short_lock_timeout
from tests.support.domain import NOW, System
from tests.support.plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 10)
SNAPSHOT = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"


def plant_path(system: System, plant: Plant) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"


def region_path(system: System) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"


@pytest.mark.parametrize("model", [Region, Plant, ProviderAccount])
def test_forecast_reads_do_not_wait_for_row_locks_held_elsewhere(
    monkeypatch: pytest.MonkeyPatch, model: type[Model]
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, DAY, collected_at=NOW))
    pk = {Region: system.region.pk, Plant: plant.pk, ProviderAccount: system.account.pk}[model]
    client = Client()
    client.force_login(system.member)
    with row_locked_elsewhere(model, pk), short_lock_timeout():
        plant_response = client.get(plant_path(system, plant), {"date": DAY.isoformat()})
        region_response = client.get(region_path(system), {"date": DAY.isoformat()})
    assert plant_response.status_code == 200
    assert plant_response.json()["result"] is not None
    assert region_response.status_code == 200
    assert region_response.json()["result"] is not None


@pytest.mark.parametrize(
    ("scope", "params"),
    [
        ("plant", {"date": DAY.isoformat()}),
        ("plant", {"date": DAY.isoformat(), "data_source": "auto"}),
        ("plant", {"date": NOW.date().isoformat(), "data_source": "estimated_actuals"}),
        (
            "plant",
            {"date": (DAY - timedelta(days=3)).isoformat(), "data_source": "archived_forecast"},
        ),
        ("plant", {"date": (DAY - timedelta(days=3)).isoformat(), "data_source": "auto"}),
        ("plant", {"date": DAY.isoformat(), "as_of": NOW.isoformat()}),
        ("region", {"date": DAY.isoformat()}),
        ("region", {"date": DAY.isoformat(), "data_source": "auto"}),
        ("region", {"date": (DAY - timedelta(days=3)).isoformat(), "data_source": "auto"}),
    ],
)
def test_forecast_reads_run_on_one_read_only_snapshot_without_row_locks(
    monkeypatch: pytest.MonkeyPatch, scope: str, params: dict[str, str]
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, DAY, collected_at=NOW))
    client = Client()
    client.force_login(system.member)
    path = plant_path(system, plant) if scope == "plant" else region_path(system)
    with CaptureQueriesContext(connection) as queries:
        response = client.get(path, params)
    assert response.status_code == 200
    statements = [query["sql"] for query in queries.captured_queries]
    assert not [sql for sql in statements if "FOR UPDATE" in sql or "FOR SHARE" in sql]
    assert SNAPSHOT in statements


def _incident(system: System, plant: Plant) -> None:
    start, _ = local_day_bounds(date(2026, 11, 4), system.binding.timezone)
    save_incident(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        kind="inverter",
        title="Outage",
        notes="",
        starts_at=start + timedelta(hours=9),
        ends_at=None,
        available_dc_mw=65,
        available_inverter_mw=None,
        available_export_mw=None,
    )


def _revision(system: System, plant: Plant) -> None:
    revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        PlantParameters(name="Revised", dc_capacity_mw=260),
    )


def _publication(system: System, _: Plant) -> None:
    publish_snapshot(snapshot_for(system, DAY, collected_at=NOW + timedelta(seconds=1)))


WRITERS: dict[str, Callable[[System, Plant], object]] = {
    "incident": _incident,
    "publication": _publication,
    "revision": _revision,
    "suspension": lambda system, _: Region.objects.filter(pk=system.region.pk).update(
        status="suspended"
    ),
    "account": lambda system, _: ProviderAccount.objects.filter(pk=system.account.pk).update(
        enabled=False
    ),
    "membership": lambda system, _: Membership.objects.filter(
        user=system.member, tenant=system.tenant
    ).delete(),
}


def _paused_read(system: System, plant: Plant, reached: Event, resume: Event) -> dict[str, object]:
    def pause_at_incidents(execute: Any, sql: str, *args: Any) -> Any:
        if '"incidents_' in sql and not reached.is_set():
            reached.set()
            assert resume.wait(timeout=10)
        return execute(sql, *args)

    try:
        with connection.execute_wrapper(pause_at_incidents):
            return plant_outlook(
                system.member,
                system.tenant.pk,
                system.region.pk,
                plant.pk,
                target_date=DAY,
                resolution_minutes=60,
            )
    finally:
        connection.close()


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_open_forecast_read_lets_writers_commit_and_keeps_its_snapshot(
    monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    activate_calibration(*scope, ready_run(system, plant).pk)
    published = publish_snapshot(snapshot_for(system, DAY, collected_at=NOW))
    original_configuration = plant.current_configuration_id
    reached, resume = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        reader: Future[dict[str, object]] = pool.submit(
            _paused_read, system, plant, reached, resume
        )
        try:
            assert reached.wait(timeout=10)
            with short_lock_timeout():
                WRITERS[writer](system, plant)
        finally:
            resume.set()
        during = reader.result(timeout=10)
    during_result = cast(dict[str, Any], during["result"])
    assert during["configuration_id"] == original_configuration
    assert during_result["factor"] == 0.5
    assert during_result["provenance"]["snapshot_id"] == published.pk
    assert during["outage_scenario"] is None
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(seconds=1))
    if writer == "membership":
        with pytest.raises(PermissionDenied):
            plant_outlook(*scope, target_date=DAY, resolution_minutes=60)
        return
    after = plant_outlook(*scope, target_date=DAY, resolution_minutes=60)
    if writer == "incident":
        assert cast(dict[str, Any], after["result"])["factor"] == 1
        assert after["calibration_id"] is None
        assert after["outage_scenario"] is not None
    elif writer == "publication":
        after_result = cast(dict[str, Any], after["result"])
        assert after_result["provenance"]["snapshot_id"] != published.pk
        assert after_result["factor"] == 0.5
    elif writer == "revision":
        plant.refresh_from_db()
        assert after["configuration_id"] == plant.current_configuration_id
        assert after["configuration_id"] != original_configuration
    else:
        assert after["unavailable_reason"] == "configuration_unavailable"
