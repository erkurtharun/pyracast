from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from django.core.management import CommandError, call_command
from dramatiq import Worker
from dramatiq.brokers.stub import StubBroker

from pyracast import runtime
from pyracast.forecasts.models import CollectionJob, ForecastResult, RequestAttempt
from tests.test_domain import make_system


@pytest.mark.django_db(transaction=True)
def test_stub_queue_duplicate_delivery_is_idempotent() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
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
    assert runtime.dispatch(actor) == 2
    assert runtime.dispatch(actor) == 2
    assert broker.queues[actor.queue_name].qsize() == 4
    assert scheduled.call_count == 2
    assert pending.call_args.args == scheduled.call_args.args


def test_rabbitmq_broker_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    constructor = MagicMock()
    monkeypatch.setattr(runtime, "RabbitmqBroker", constructor)
    monkeypatch.setenv("PYRACAST_RABBITMQ_URL", "amqp://example.invalid/test")
    runtime.make_broker()
    constructor.assert_called_once_with(url="amqp://example.invalid/test", confirm_delivery=True)
    monkeypatch.delenv("PYRACAST_RABBITMQ_URL")
    runtime.make_broker()
    assert constructor.call_args.kwargs["url"] == "amqp://guest:guest@127.0.0.1:5672/%2F"


def test_dispatch_command_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyracast.forecasts.management.commands import dispatch as command

    broker, actor, send = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    monkeypatch.setattr(command, "make_actor", lambda _: actor)
    monkeypatch.setattr(command, "dispatch", send)
    calibration_actor, send_calibrations = MagicMock(), MagicMock()
    monkeypatch.setattr(command, "make_calibration_actor", lambda _: calibration_actor)
    monkeypatch.setattr(command, "dispatch_calibrations", send_calibrations)
    call_command("dispatch")
    send.assert_called_once_with(actor)
    send_calibrations.assert_called_once_with(calibration_actor)
    broker.close.assert_called_once()
    with pytest.raises(CommandError, match="positive"):
        call_command("dispatch", poll_seconds=0)
    monkeypatch.setattr(
        command, "time", SimpleNamespace(sleep=MagicMock(side_effect=KeyboardInterrupt))
    )
    call_command("dispatch", loop=True, poll_seconds=1)
    assert send.call_count == 2
    assert send_calibrations.call_count == 2
    assert broker.close.call_count == 2


def test_worker_command_stops_on_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyracast.forecasts.management.commands import worker as command

    broker, actor, worker = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    monkeypatch.setattr(command, "make_actor", lambda _: actor)
    calibration_registration = MagicMock()
    monkeypatch.setattr(command, "make_calibration_actor", calibration_registration)
    create_worker = MagicMock(return_value=worker)
    monkeypatch.setattr(command, "Worker", create_worker)
    event = MagicMock()
    event.wait.side_effect = KeyboardInterrupt
    monkeypatch.setattr(command, "threading", SimpleNamespace(Event=lambda: event))
    call_command("worker", threads=2)
    create_worker.assert_called_once_with(broker, worker_threads=2)
    calibration_registration.assert_called_once_with(broker)
    worker.start.assert_called_once()
    worker.stop.assert_called_once()
    broker.close.assert_called_once()
    with pytest.raises(CommandError, match="positive"):
        call_command("worker", threads=0)
