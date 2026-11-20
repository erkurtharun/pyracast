import logging
import threading
import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import dramatiq
import pytest
from django.conf import LazySettings
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from dramatiq import Actor, Broker
from dramatiq.brokers.rabbitmq import RabbitmqBroker
from dramatiq.brokers.stub import StubBroker
from dramatiq.middleware.time_limit import TimeLimitExceeded

from pyracast.background import broker as broker_module
from pyracast.background.failures import PhaseOutcome, queue_depth, queue_full
from pyracast.forecasts import runtime
from pyracast.forecasts.management.commands import worker as command
from pyracast.metering.runtime import dispatch_epias, make_epias_actor
from pyracast.notifications.runtime import make_outbound_mail_actor, publish_outbound_mail
from pyracast.shutdown import ShutdownRequest

FACTORIES = (
    "make_actor",
    "make_calibration_actor",
    "make_notification_actor",
    "make_outbound_mail_actor",
    "make_epias_actor",
)


def test_the_worker_arms_each_actor_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    broker = StubBroker()
    interrupted = threading.Event()

    def overrun() -> None:
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                time.sleep(0.01)
        except TimeLimitExceeded:
            interrupted.set()
            raise

    def register(target: Broker) -> None:
        dramatiq.actor(broker=target, actor_name="overrun", time_limit=50, max_retries=0)(
            overrun
        ).send()

    monkeypatch.setattr(command, "make_broker", lambda: broker)
    for factory in FACTORIES[1:]:
        monkeypatch.setattr(command, factory, MagicMock())
    monkeypatch.setattr(command, "make_actor", register)
    monkeypatch.setattr(ShutdownRequest, "wait", lambda _self, _timeout=None: interrupted.wait(3))

    call_command("worker", threads=1)

    assert interrupted.is_set()


def test_the_worker_drains_for_ten_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, worker = MagicMock(), MagicMock()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    for factory in FACTORIES:
        monkeypatch.setattr(command, factory, MagicMock())
    monkeypatch.setattr(command, "Worker", MagicMock(return_value=worker))
    monkeypatch.setattr(ShutdownRequest, "wait", MagicMock(return_value=True))

    call_command("worker")

    worker.stop.assert_called_once_with(timeout=600_000)
    broker.emit_after.assert_called_once_with("process_boot")


@pytest.mark.parametrize("configured", ["", "  "])
def test_a_missing_broker_url_stops_production_processes_at_start(
    settings: LazySettings, monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    monkeypatch.delenv("PYRACAST_RABBITMQ_URL", raising=False)
    settings.DEBUG = False
    settings.PYRACAST_RABBITMQ_URL = configured

    with pytest.raises(ImproperlyConfigured, match="PYRACAST_RABBITMQ_URL"):
        broker_module.make_broker()
    with pytest.raises(ImproperlyConfigured, match="PYRACAST_RABBITMQ_URL"):
        call_command("worker")


def test_the_deployment_check_refuses_a_release_without_a_broker_url(
    settings: LazySettings,
) -> None:
    settings.DEBUG = False
    settings.PYRACAST_RABBITMQ_URL = ""
    refused = {message.id for message in run_checks(include_deployment_checks=True)}
    settings.PYRACAST_RABBITMQ_URL = "amqp://pyracast:secret@rabbitmq:5672/%2F"
    accepted = {message.id for message in run_checks(include_deployment_checks=True)}

    assert "background.E001" in refused
    assert "background.E001" not in accepted


def test_development_uses_the_local_broker_without_a_url(
    settings: LazySettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    constructor = MagicMock()
    monkeypatch.setattr(broker_module, "RabbitmqBroker", constructor)
    monkeypatch.delenv("PYRACAST_RABBITMQ_URL", raising=False)
    settings.DEBUG = True
    settings.PYRACAST_RABBITMQ_URL = ""

    broker_module.make_broker()

    assert constructor.call_args.kwargs["url"].startswith("amqp://guest:guest@127.0.0.1:5672/%2F?")


def test_a_queue_ten_batches_deep_receives_no_more_collections(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    broker = StubBroker()
    actor = runtime.make_actor(broker)
    for identifier in range(1000):
        actor.send(identifier)
    monkeypatch.setattr(runtime, "enqueue_due", MagicMock())
    monkeypatch.setattr(runtime, "pending_job_ids", MagicMock(return_value=[3, 5]))

    outcome = runtime.dispatch_collections(actor)

    assert outcome == PhaseOutcome(published=0)
    assert broker.queues[actor.queue_name].qsize() == 1000
    assert f"publication_skipped queue={actor.queue_name} depth=1000" in pyracast_logs.text


def test_a_queue_below_ten_batches_still_receives_its_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    broker = StubBroker()
    actor = make_epias_actor(broker)
    for identifier in range(999):
        actor.send(identifier)
    monkeypatch.setattr("pyracast.metering.runtime.enqueue_due_epias_jobs", MagicMock())
    monkeypatch.setattr(
        "pyracast.metering.runtime.pending_epias_ids", MagicMock(return_value=[3, 5])
    )

    assert dispatch_epias(actor) == 2
    assert broker.queues[actor.queue_name].qsize() == 1001


def test_a_queue_ten_batches_deep_receives_no_more_metering_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = StubBroker()
    actor = make_epias_actor(broker)
    for identifier in range(1000):
        actor.send(identifier)
    pending = MagicMock(return_value=[3, 5])
    monkeypatch.setattr("pyracast.metering.runtime.enqueue_due_epias_jobs", MagicMock())
    monkeypatch.setattr("pyracast.metering.runtime.pending_epias_ids", pending)

    assert dispatch_epias(actor) == 0
    assert broker.queues[actor.queue_name].qsize() == 1000


def test_a_queue_ten_batches_deep_receives_no_more_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    broker = StubBroker()
    actor = make_outbound_mail_actor(broker)
    for identifier in range(1000):
        actor.send(identifier)
    expired, publish = MagicMock(return_value=0), MagicMock(return_value=2)
    monkeypatch.setattr("pyracast.notifications.runtime.end_expired_mail", expired)
    monkeypatch.setattr("pyracast.notifications.runtime.publish_due_mail", publish)

    assert publish_outbound_mail(actor) == 0
    expired.assert_called_once()
    publish.assert_not_called()
    broker.queues[actor.queue_name].get_nowait()

    assert publish_outbound_mail(actor) == 2


def test_the_queue_depth_comes_from_the_broker_holding_the_queue(
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    rabbitmq = MagicMock(spec=RabbitmqBroker)
    rabbitmq.get_queue_message_counts.return_value = (1000, 0, 0)
    deep = SimpleNamespace(broker=rabbitmq, queue_name="epias")
    unknown = SimpleNamespace(broker=MagicMock(), queue_name="epias")

    assert queue_depth(cast("Actor[[int], None]", deep)) == 1000
    assert queue_full(cast("Actor[[int], None]", deep), logging.getLogger("pyracast.test"))
    rabbitmq.get_queue_message_counts.assert_called_with("epias")
    assert queue_depth(cast("Actor[[int], None]", unknown)) is None
    assert not queue_full(cast("Actor[[int], None]", unknown), logging.getLogger("pyracast.test"))
    assert "publication_skipped queue=epias depth=1000" in pyracast_logs.text
