import contextlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone
from dramatiq import Message
from dramatiq.brokers.stub import StubBroker

from pyracast import trash
from pyracast.epias import AUTH_URL, BASE_URL, EpiasClient
from pyracast.forecasts.management.commands import dispatch as command
from pyracast.forecasts.models import DispatcherStatus, ForecastSnapshot
from pyracast.metering import scheduler as metering_scheduler
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection
from pyracast.metering.scheduler import enqueue_due_epias_jobs
from pyracast.plants import automation
from pyracast.plants.models import CalibrationRun, Plant, PlantEvent
from pyracast.plants.operations import request_calibration
from pyracast.plants.preparation import prepare_calibration
from pyracast.regions.models import Region
from pyracast.trash import RETENTION, restore_region, trash_region
from tests.support.domain import NOW, System
from tests.support.epias import PASSWORD, TICKET, USERNAME
from tests.support.errors import raises_code
from tests.support.metering import NEXT_NIGHT, PROVIDER_PLANT, connect, connected
from tests.support.plant_operations import make_plant, snapshot_for
from tests.support.trash_operations import sibling, sibling_plant

pytestmark = pytest.mark.django_db(transaction=True)

LIST_URL = f"{BASE_URL}/injection-quantity-powerplant-list"


def dispatch_once(monkeypatch: pytest.MonkeyPatch) -> StubBroker:
    broker = StubBroker()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    with contextlib.suppress(CommandError):
        call_command("dispatch")
    return broker


def published(broker: StubBroker, queue: str) -> list[int]:
    return [Message.decode(data).args[0] for data in broker.queues[queue].queue]


def automate(plant: Plant, system: System, due: datetime) -> None:
    Plant.objects.filter(pk=plant.pk).update(
        automatic_calibration_enabled=True,
        automatic_calibration_enabled_by=system.member,
        next_calibration_check_at=due,
    )


def test_one_dispatch_run_publishes_other_plants_and_backs_off_a_failing_plant(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system, failing = make_plant(monkeypatch)
    healthy = sibling_plant(system)
    assert healthy.current_configuration_id is not None
    run = request_calibration(
        system.member,
        system.tenant.pk,
        healthy.region_id,
        healthy.pk,
        configuration_id=healthy.current_configuration_id,
        start_date=NOW.date() - timedelta(days=7),
        end_date=NOW.date() - timedelta(days=1),
    )
    automate(failing, system, NOW - timedelta(hours=2))
    automate(healthy, system, NOW - timedelta(hours=1))
    attempts: list[int] = []

    def prepare(plant_id: int, now: datetime | None = None) -> CalibrationRun | None:
        attempts.append(plant_id)
        if plant_id == failing.pk:
            raise ValueError("private preparation detail")
        return prepare_calibration(plant_id, now)

    monkeypatch.setattr("pyracast.plants.automation.prepare_calibration", prepare)
    broker = dispatch_once(monkeypatch)

    assert published(broker, "calibration") == [run.pk]
    assert attempts == [failing.pk, healthy.pk]
    status = DispatcherStatus.objects.get(phase="calibration")
    assert status.last_error == "items_failed:1"
    assert status.published == 1
    assert DispatcherStatus.objects.exclude(phase="calibration").filter(last_error="").count() == 4
    failing.refresh_from_db()
    assert failing.next_calibration_check_at is not None
    assert failing.next_calibration_check_at > datetime.now(UTC)
    skipped = PlantEvent.objects.filter(plant=failing, action="calibration_automatic_skipped").get()
    assert skipped.details["reason"] == "preparation_failed"
    assert f"item=plant:{failing.pk}" in pyracast_logs.text
    assert "origin=" in pyracast_logs.text
    assert "private preparation detail" not in pyracast_logs.text

    dispatch_once(monkeypatch)
    assert attempts == [failing.pk, healthy.pk]


def test_a_failing_region_purge_does_not_stop_later_regions_and_is_deferred(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system, _ = make_plant(monkeypatch)
    other, _ = sibling(system)
    trash_region(system.member, system.tenant.pk, system.region.pk)
    trash_region(system.member, system.tenant.pk, other.pk)
    Region.objects.update(deleted_at=NOW - RETENTION - timedelta(days=1))
    attempts: list[int] = []
    original = trash._purge_region

    def purge(region: Region, plants: list[Plant], budget: trash._Budget) -> None:
        attempts.append(region.pk)
        if region.pk == system.region.pk:
            raise RuntimeError("private purge detail")
        original(region, plants, budget)

    monkeypatch.setattr("pyracast.trash._purge_region", purge)
    dispatch_once(monkeypatch)

    assert attempts == [system.region.pk, other.pk]
    assert Region.objects.filter(pk=system.region.pk).exists()
    assert not Region.objects.filter(pk=other.pk).exists()
    assert DispatcherStatus.objects.get(phase="retention").last_error == "items_failed:1"
    assert f"item=region:{system.region.pk}" in pyracast_logs.text
    assert "private purge detail" not in pyracast_logs.text

    dispatch_once(monkeypatch)
    assert attempts == [system.region.pk, other.pk]


def test_a_purge_beyond_its_budget_resumes_next_run_and_later_phases_still_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    for offset in range(3):
        snapshot_for(system, NOW.date() - timedelta(days=offset + 1))
    trash_region(system.member, system.tenant.pk, system.region.pk)
    deleted_at = NOW - RETENTION - timedelta(days=1)
    Region.objects.update(deleted_at=deleted_at)
    monkeypatch.setattr("pyracast.trash.PURGE_CHUNK_SIZE", 1)
    monkeypatch.setattr("pyracast.trash.PURGE_STATEMENT_BUDGET", 2)

    dispatch_once(monkeypatch)

    assert Region.objects.filter(pk=system.region.pk).exists()
    assert ForecastSnapshot.objects.count() == 3
    assert Region.objects.get(pk=system.region.pk).deleted_at == NOW - 2 * RETENTION
    assert set(DispatcherStatus.objects.values_list("phase", "last_error")) >= {
        ("retention", ""),
        ("metering", ""),
    }
    monkeypatch.setattr(timezone, "now", lambda: deleted_at + RETENTION - timedelta(days=1))
    with raises_code("trash_retention_expired"):
        restore_region(system.member, system.tenant.pk, system.region.pk)

    monkeypatch.setattr(timezone, "now", lambda: NOW)
    monkeypatch.setattr("pyracast.trash.PURGE_STATEMENT_BUDGET", 1000)
    dispatch_once(monkeypatch)

    assert not Region.objects.filter(pk=system.region.pk).exists()
    assert not Plant.objects.filter(pk=plant.pk).exists()
    assert not ForecastSnapshot.objects.exists()


def test_a_failing_connection_does_not_stop_other_connections_or_publication(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system, _, failing = connected(monkeypatch)
    healthy = connect(sibling_plant(system), system.admin)
    original = metering_scheduler.enqueue_connection

    def enqueue(connection: EpiasConnection, now: datetime, *, initial: bool = False) -> list[int]:
        if connection.pk == failing.pk:
            raise RuntimeError("private metering detail")
        return list(original(connection, now, initial=initial))

    monkeypatch.setattr("pyracast.metering.scheduler.enqueue_connection", enqueue)
    EpiasCollectionJob.objects.filter(connection=failing).delete()
    broker = dispatch_once(monkeypatch)

    assert sorted(published(broker, "epias")) == sorted(
        EpiasCollectionJob.objects.filter(connection=healthy, status="pending").values_list(
            "pk", flat=True
        )
    )
    assert DispatcherStatus.objects.get(phase="metering").last_error == "items_failed:1"
    assert f"item=connection:{failing.pk}" in pyracast_logs.text
    assert "private metering detail" not in pyracast_logs.text


def epias_client(handler: httpx.MockTransport) -> EpiasClient:
    return EpiasClient(httpx.Client(transport=handler), username=USERNAME, password=PASSWORD)


def test_due_connections_share_one_settlement_list_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _, first = connected(monkeypatch)
    second = connect(sibling_plant(system), system.admin)
    EpiasConnection.objects.update(settlement_checked_at=None)
    lists: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        assert str(request.url) == LIST_URL
        lists.append(request)
        return httpx.Response(
            200,
            json={
                "items": [{"id": 7001, "eic": PROVIDER_PLANT.eic, "name": "n", "shortName": "s"}]
            },
        )

    client = epias_client(httpx.MockTransport(handler))
    monkeypatch.setattr("pyracast.metering.scheduler.get_epias_client", lambda: client)
    enqueue_due_epias_jobs(now=NEXT_NIGHT + timedelta(hours=1))

    assert len(lists) == 1
    assert set(
        EpiasConnection.objects.filter(pk__in=(first.pk, second.pk)).values_list(
            "settlement_plant_id", flat=True
        )
    ) == {7001}


def test_a_failed_settlement_lookup_is_retried_after_a_short_delay(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    _, _, linked = connected(monkeypatch)
    EpiasConnection.objects.update(settlement_checked_at=None)
    lists: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        lists.append(request)
        return httpx.Response(503)

    client = epias_client(httpx.MockTransport(handler))
    monkeypatch.setattr("pyracast.metering.scheduler.get_epias_client", lambda: client)
    failed_at = NEXT_NIGHT + timedelta(hours=1)
    enqueue_due_epias_jobs(now=failed_at)
    assert len(lists) == 1
    assert "code=epias_request_failed" in pyracast_logs.text
    linked.refresh_from_db()
    assert linked.settlement_plant_id is None
    assert linked.settlement_checked_at is not None

    enqueue_due_epias_jobs(now=failed_at + timedelta(minutes=30))
    assert len(lists) == 1
    enqueue_due_epias_jobs(now=failed_at + timedelta(hours=1))
    assert len(lists) == 2


def test_a_due_check_backs_off_only_a_plant_that_is_still_automated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    automation._defer_due_check((plant.pk, plant.region_id, system.tenant.pk), NOW)
    plant.refresh_from_db()
    assert plant.next_calibration_check_at is None
    assert not PlantEvent.objects.filter(
        plant=plant, action="calibration_automatic_skipped"
    ).exists()
