import signal
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.management import CommandError, call_command
from dramatiq import Worker
from dramatiq.brokers.stub import StubBroker

from pyracast import runtime
from pyracast.forecasts.models import CollectionJob, ForecastResult, RequestAttempt
from pyracast.forecasts.monitoring import PhaseOutcome
from pyracast.shutdown import ShutdownRequest
from tests.support.domain import make_system


@pytest.fixture(autouse=True)
def isolated_dispatch_monitoring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pyracast.forecasts.management.commands.dispatch.record_dispatch_phase", MagicMock()
    )


@pytest.mark.django_db(transaction=True)
def test_stub_queue_duplicate_delivery_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    monkeypatch.setattr(
        runtime, "datetime", MagicMock(now=MagicMock(return_value=job.scheduled_slot))
    )
    broker = StubBroker()
    actor = runtime.make_actor(broker)
    worker = Worker(broker, worker_threads=1, worker_timeout=100)
    worker.start()
    try:
        actor.send(job.pk)
        actor.send(job.pk)
        broker.join(actor.queue_name, timeout=10000, fail_fast=True)
        worker.join()
    finally:
        worker.stop()
        broker.close()
    job.refresh_from_db()
    assert job.status == "succeeded"
    assert ForecastResult.objects.filter(snapshot__job=job).count() == 1
    assert RequestAttempt.objects.filter(job=job).count() == 1


def test_dispatch_schedules_before_sending_and_repeats_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled = MagicMock()
    pending = MagicMock(return_value=[3, 5])
    monkeypatch.setattr(runtime, "enqueue_due", scheduled)
    monkeypatch.setattr(runtime, "pending_job_ids", pending)
    broker = StubBroker()
    actor = runtime.make_actor(broker)
    assert runtime.dispatch(actor) == PhaseOutcome(published=2)
    assert runtime.dispatch(actor) == PhaseOutcome(published=2)
    assert broker.queues[actor.queue_name].qsize() == 4
    assert scheduled.call_count == 2
    assert pending.call_args.args == scheduled.call_args.args


def test_rabbitmq_broker_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    constructor = MagicMock()
    monkeypatch.setattr(runtime, "RabbitmqBroker", constructor)
    monkeypatch.setenv("PYRACAST_RABBITMQ_URL", "amqp://example.invalid/test")
    runtime.make_broker()
    parameters = parse_qs(urlsplit(constructor.call_args.kwargs["url"]).query)
    assert parameters == {
        "socket_timeout": ["5"],
        "stack_timeout": ["10"],
        "blocked_connection_timeout": ["10"],
        "heartbeat": ["30"],
        "connection_attempts": ["1"],
    }
    assert constructor.call_args.kwargs["confirm_delivery"] is True
    monkeypatch.delenv("PYRACAST_RABBITMQ_URL")
    runtime.make_broker()
    assert constructor.call_args.kwargs["url"].startswith("amqp://guest:guest@127.0.0.1:5672/%2F?")


def test_rabbitmq_failover_urls_preserve_explicit_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PYRACAST_RABBITMQ_URL",
        "amqp://node1.invalid/?heartbeat=60;amqp://node2.invalid/?socket_timeout=8",
    )
    broker = runtime.make_broker()
    first, second = broker.parameters
    assert first.host == "node1.invalid"
    assert first.heartbeat == 60
    assert first.socket_timeout == 5
    assert second.host == "node2.invalid"
    assert second.socket_timeout == 8
    assert second.blocked_connection_timeout == 10
    broker.close()


def test_dispatch_command_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyracast.forecasts.management.commands import dispatch as command

    broker, actor, purge = MagicMock(), MagicMock(), MagicMock()
    send = MagicMock(return_value=PhaseOutcome())
    monkeypatch.setattr(command, "purge_expired", purge)
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    monkeypatch.setattr(command, "make_actor", lambda _: actor)
    monkeypatch.setattr(command, "dispatch", send)
    calibration_actor, send_calibrations = MagicMock(), MagicMock()
    monkeypatch.setattr(command, "make_calibration_actor", lambda _: calibration_actor)
    monkeypatch.setattr(command, "dispatch_calibrations", send_calibrations)
    notification_actor, send_notifications = MagicMock(), MagicMock()
    monkeypatch.setattr(command, "make_notification_actor", lambda _: notification_actor)
    monkeypatch.setattr(command, "dispatch_notifications", send_notifications)
    epias_actor, send_epias = MagicMock(), MagicMock()
    monkeypatch.setattr(command, "make_epias_actor", lambda _: epias_actor)
    monkeypatch.setattr(command, "dispatch_epias", send_epias)
    call_command("dispatch")
    purge.assert_called_once_with()
    send.assert_called_once_with(actor)
    send_calibrations.assert_called_once_with(calibration_actor)
    send_notifications.assert_called_once_with(notification_actor)
    send_epias.assert_called_once_with(epias_actor)
    broker.close.assert_called_once()
    with pytest.raises(CommandError, match="positive"):
        call_command("dispatch", poll_seconds=0)
    monkeypatch.setattr(ShutdownRequest, "wait", MagicMock(side_effect=KeyboardInterrupt))
    call_command("dispatch", loop=True, poll_seconds=1)
    assert send.call_count == 2
    assert purge.call_count == 2
    assert send_calibrations.call_count == 2
    assert send_notifications.call_count == 2
    assert send_epias.call_count == 2
    assert broker.close.call_count == 2


def test_worker_command_stops_on_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyracast.forecasts.management.commands import worker as command

    broker, actor, worker = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    monkeypatch.setattr(command, "make_actor", lambda _: actor)
    calibration_registration = MagicMock()
    monkeypatch.setattr(command, "make_calibration_actor", calibration_registration)
    notification_registration = MagicMock()
    monkeypatch.setattr(command, "make_notification_actor", notification_registration)
    epias_registration = MagicMock()
    monkeypatch.setattr(command, "make_epias_actor", epias_registration)
    create_worker = MagicMock(return_value=worker)
    monkeypatch.setattr(command, "Worker", create_worker)
    monkeypatch.setattr(ShutdownRequest, "wait", MagicMock(side_effect=KeyboardInterrupt))
    call_command("worker", threads=2)
    create_worker.assert_called_once_with(
        broker, worker_threads=2, queues={"default", "calibration", "epias"}
    )
    calibration_registration.assert_called_once_with(broker)
    notification_registration.assert_called_once_with(broker)
    epias_registration.assert_called_once_with(broker)
    worker.start.assert_called_once()
    worker.stop.assert_called_once()
    broker.close.assert_called_once()
    with pytest.raises(CommandError, match="positive"):
        call_command("worker", threads=0)


def test_notification_worker_consumes_only_its_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyracast.forecasts.management.commands import worker as command

    broker, worker = MagicMock(), MagicMock()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    for factory in (
        "make_actor",
        "make_calibration_actor",
        "make_notification_actor",
        "make_epias_actor",
    ):
        monkeypatch.setattr(command, factory, MagicMock())
    constructor = MagicMock(return_value=worker)
    monkeypatch.setattr(command, "Worker", constructor)
    monkeypatch.setattr(ShutdownRequest, "wait", MagicMock(side_effect=KeyboardInterrupt))
    call_command("worker", threads=2, queues=["notifications"])
    constructor.assert_called_once_with(broker, worker_threads=2, queues={"notifications"})
    worker.stop.assert_called_once()


@pytest.mark.parametrize("command_name", ["worker", "dispatch"])
def test_commands_handle_termination_and_restore_previous_handlers(
    command_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyracast.forecasts.management.commands import dispatch, worker

    command = worker if command_name == "worker" else dispatch
    broker = MagicMock()
    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    for factory in (
        "make_actor",
        "make_calibration_actor",
        "make_notification_actor",
        "make_epias_actor",
    ):
        monkeypatch.setattr(command, factory, MagicMock())

    def terminate() -> None:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    if command_name == "worker":
        instance = MagicMock()
        instance.start.side_effect = terminate
        instance.stop.side_effect = terminate
        monkeypatch.setattr(command, "Worker", lambda *args, **kwargs: instance)
        call_command(command_name)
        instance.stop.assert_called_once()
    else:
        monkeypatch.setattr(command, "purge_expired", terminate)
        for operation in (
            "dispatch",
            "dispatch_calibrations",
            "dispatch_notifications",
            "dispatch_epias",
        ):
            monkeypatch.setattr(command, operation, MagicMock())
        call_command(command_name, loop=True, poll_seconds=60)

    broker.close.assert_called_once()
    assert signal.getsignal(signal.SIGTERM) == previous


def test_worker_command_closes_broker_when_stopping_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyracast.forecasts.management.commands import worker as command

    broker, worker = MagicMock(), MagicMock()
    worker.start.side_effect = KeyboardInterrupt
    worker.stop.side_effect = RuntimeError("shutdown failed")
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    monkeypatch.setattr(command, "Worker", lambda *args, **kwargs: worker)
    for factory in (
        "make_actor",
        "make_calibration_actor",
        "make_notification_actor",
        "make_epias_actor",
    ):
        monkeypatch.setattr(command, factory, MagicMock())

    with pytest.raises(RuntimeError, match="shutdown failed"):
        call_command("worker")

    broker.close.assert_called_once()


@pytest.mark.parametrize("interval", [float("nan"), float("inf"), -float("inf")])
def test_dispatch_rejects_nonfinite_poll_interval(interval: float) -> None:
    with pytest.raises(CommandError, match="positive and finite"):
        call_command("dispatch", poll_seconds=interval)


def test_dispatch_does_not_start_cycle_after_shutdown_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyracast.forecasts.management.commands import dispatch as command

    broker, purge = MagicMock(), MagicMock()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    monkeypatch.setattr(command, "purge_expired", purge)
    for factory in ("make_actor", "make_calibration_actor", "make_notification_actor"):
        monkeypatch.setattr(command, factory, MagicMock())

    def finish_registration(_: object) -> None:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(command, "make_epias_actor", finish_registration)

    call_command("dispatch", loop=True)

    purge.assert_not_called()
    broker.close.assert_called_once()


@pytest.mark.parametrize("command_name", ["worker", "dispatch"])
def test_existing_stop_file_prevents_command_startup(
    command_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyracast.forecasts.management.commands import dispatch, worker

    command = worker if command_name == "worker" else dispatch
    stop_file = tmp_path / "stop"
    stop_file.touch()
    broker = MagicMock()
    monkeypatch.setattr(command, "make_broker", broker)

    call_command(command_name, stop_file=stop_file)

    broker.assert_not_called()


def test_dispatch_repeats_after_poll_timeout_until_stop_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyracast.forecasts.management.commands import dispatch as command

    broker = MagicMock()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    for factory in (
        "make_actor",
        "make_calibration_actor",
        "make_notification_actor",
        "make_epias_actor",
    ):
        monkeypatch.setattr(command, factory, MagicMock())
    for operation in (
        "purge_expired",
        "dispatch",
        "dispatch_calibrations",
        "dispatch_notifications",
    ):
        monkeypatch.setattr(command, operation, MagicMock())
    stop_file = tmp_path / "stop"
    cycles: list[str] = []

    def finish_cycle(_: object) -> None:
        cycles.append("completed")
        if len(cycles) == 2:
            stop_file.touch()

    monkeypatch.setattr(command, "dispatch_epias", finish_cycle)

    call_command("dispatch", loop=True, poll_seconds=0.001, stop_file=stop_file)

    assert cycles == ["completed", "completed"]
    broker.close.assert_called_once()
