import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import psycopg
import pytest
from django.db import close_old_connections, connection
from dramatiq.brokers.stub import StubBroker

from pyracast.incidents.models import PlantIncident
from pyracast.plants import preparation, training
from pyracast.plants.automation import set_calibration_automation
from pyracast.plants.models import CalibrationRun, Plant, PlantEvent, ProductionObservation
from pyracast.plants.runtime import dispatch_calibrations, make_calibration_actor
from tests.support.calibration_preparation import add_days
from tests.support.domain import NOW, System
from tests.support.incident_calibration import incident
from tests.support.migrations import forward, leaf_nodes, migrate
from tests.support.plant_operations import make_plant
from tests.support.trash_operations import sibling_plant

pytestmark = pytest.mark.django_db(transaction=True)


class Crash(BaseException):
    pass


def raise_private(*args: object, **kwargs: object) -> None:
    raise RuntimeError("private preparation detail")


def write_observations(system: System, plant: Plant) -> None:
    add_days(system, plant, 1, 1)


def enable_automation(system: System, plant: Plant) -> None:
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )


def declare_outage(system: System, plant: Plant) -> None:
    incident(system, plant, NOW - timedelta(days=2), NOW - timedelta(days=1))


WRITERS: dict[str, tuple[Callable[[System, Plant], None], Callable[[Plant], bool]]] = {
    "observations": (write_observations, lambda plant: ProductionObservation.objects.exists()),
    "automation": (
        enable_automation,
        lambda plant: Plant.objects.get(pk=plant.pk).automatic_calibration_enabled,
    ),
    "outage": (declare_outage, lambda plant: PlantIncident.objects.filter(plant=plant).exists()),
}


def committed_request(plant_id: int) -> datetime | None:
    with (
        psycopg.connect(**connection.get_connection_params(), autocommit=True) as other,
        other.cursor() as cursor,
    ):
        cursor.execute("SET TIME ZONE 'UTC'")
        cursor.execute(
            "SELECT calibration_preparation_due_at FROM plants_plant WHERE id = %s", (plant_id,)
        )
        row = cursor.fetchone()
    assert row is not None
    value: datetime | None = row[0]
    return value


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_each_writer_commits_while_preparation_raises(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture, writer: str
) -> None:
    system, plant = make_plant(monkeypatch)
    write, persisted = WRITERS[writer]
    monkeypatch.setattr(preparation, "latest_measurement_day", raise_private)

    write(system, plant)

    assert persisted(plant)
    retry_at = committed_request(plant.pk)
    assert retry_at is not None
    assert retry_at > NOW
    assert f"calibration_preparation_failed item=plant:{plant.pk}" in pyracast_logs.text
    assert "private preparation detail" not in pyracast_logs.text


@pytest.mark.parametrize("step", ["promotion", "preparation"])
def test_a_finished_calibration_persists_while_its_follow_up_raises(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture, step: str
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 7, daylight_only=True)
    run = CalibrationRun.objects.get()
    Plant.objects.filter(pk=plant.pk).update(
        automatic_calibration_enabled=True,
        automatic_calibration_enabled_by=system.member,
        next_calibration_check_at=NOW + timedelta(days=1),
    )
    if step == "promotion":
        monkeypatch.setattr(
            "pyracast.plants.automation.promote_automatic_calibration", raise_private
        )
    else:
        monkeypatch.setattr(preparation, "latest_measurement_day", raise_private)

    assert training.run_calibration(run.pk, NOW)

    run.refresh_from_db()
    assert run.status == "ready"
    assert run.last_error == ""
    if step == "promotion":
        skipped = PlantEvent.objects.get(plant=plant, action="calibration_automatic_skipped")
        assert skipped.details["reason"] == "promotion_failed"
    else:
        assert committed_request(plant.pk) is not None
    assert "private preparation detail" not in pyracast_logs.text


def crash_after_write(monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant) -> None:
    def crash(*args: object, **kwargs: object) -> None:
        raise Crash

    with monkeypatch.context() as patched:
        patched.setattr(preparation, "latest_measurement_day", crash)
        with pytest.raises(Crash):
            write_observations(system, plant)


def test_a_crash_between_the_write_and_its_preparation_loses_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    crash_after_write(monkeypatch, system, plant)

    assert ProductionObservation.objects.filter(plant=plant).exists()
    assert committed_request(plant.pk) == NOW
    assert not CalibrationRun.objects.exists()

    broker = StubBroker()
    assert dispatch_calibrations(make_calibration_actor(broker)) == 1
    run = CalibrationRun.objects.get()
    assert run.status == "pending"
    assert committed_request(plant.pk) is None


def test_concurrent_dispatchers_prepare_one_request_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    crash_after_write(monkeypatch, system, plant)
    actor = make_calibration_actor(StubBroker())
    original = preparation.prepare_calibration
    attempts: list[int] = []
    barrier = Barrier(2)

    def slow(plant_id: int, now: datetime | None = None) -> CalibrationRun | None:
        attempts.append(plant_id)
        time.sleep(0.3)
        return original(plant_id, now)

    monkeypatch.setattr(preparation, "prepare_calibration", slow)

    def dispatch() -> int:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return dispatch_calibrations(actor)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: dispatch(), range(2)))
    assert attempts == [plant.pk]
    assert CalibrationRun.objects.count() == 1
    assert committed_request(plant.pk) is None


@pytest.fixture
def leaf() -> Iterator[None]:
    targets = leaf_nodes()
    try:
        yield
    finally:
        migrate(*targets)


@pytest.mark.usefixtures("leaf")
def test_migration_requests_preparation_for_every_configured_plant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, configured = make_plant(monkeypatch)
    unconfigured = sibling_plant(system)
    Plant.objects.filter(pk=unconfigured.pk).update(current_configuration=None)
    migrate(("plants", "0004_schema_naming_convention"))
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM plants_plant")
        assert cursor.fetchone() == (2,)

    forward()

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, calibration_preparation_due_at IS NOT NULL FROM plants_plant ORDER BY id"
        )
        assert cursor.fetchall() == [(configured.pk, True), (unconfigured.pk, False)]


def test_a_request_is_prepared_only_while_it_is_due(monkeypatch: pytest.MonkeyPatch) -> None:
    _, plant = make_plant(monkeypatch)
    assert preparation._prepare_requested(plant.pk + 1000, NOW) is None
    assert preparation._prepare_requested(plant.pk, NOW) is None
    later = NOW + timedelta(hours=1)
    Plant.objects.filter(pk=plant.pk).update(calibration_preparation_due_at=later)
    assert preparation._prepare_requested(plant.pk, NOW) is None
    assert committed_request(plant.pk) == later
    assert not CalibrationRun.objects.exists()
