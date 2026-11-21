import os
import uuid
from threading import Thread
from types import SimpleNamespace
from unittest.mock import MagicMock

import dramatiq
import pytest
from django.core import mail
from dramatiq import Worker
from dramatiq.brokers.stub import StubBroker

from pyracast.background.broker import make_broker
from pyracast.forecasts.models import CollectionJob, RequestAttempt
from pyracast.notifications import runtime
from pyracast.notifications.models import NotificationDelivery
from pyracast.orchestration.kinds import OUTBOUND_MAIL, REMINDER
from tests.support.notifications import SLOT, ready

pytestmark = pytest.mark.django_db(transaction=True)


def test_notification_queue_delivers_idempotently(monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = ready()
    monkeypatch.setattr(runtime, "datetime", SimpleNamespace(now=lambda _: SLOT))
    broker = StubBroker()
    actor = REMINDER.make_actor(broker)
    worker = Worker(broker, worker_threads=1, worker_timeout=100)
    worker.start()
    try:
        actor.send(delivery.pk)
        actor.send(delivery.pk)
        broker.join(actor.queue_name, timeout=10000, fail_fast=True)
        worker.join()
    finally:
        worker.stop()
        broker.close()
    delivery.refresh_from_db()
    assert delivery.status == "sent"
    assert delivery.attempts == 1


def test_dispatch_recovers_pending_and_uses_injected_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = ready()
    monkeypatch.setattr(runtime, "datetime", SimpleNamespace(now=lambda _: SLOT))
    broker = StubBroker()
    actor = REMINDER.make_actor(broker)
    mail_actor = OUTBOUND_MAIL.make_actor(broker)
    try:
        assert runtime.dispatch_notifications(actor, mail_actor, SLOT) == 1
        assert runtime.dispatch_notifications(actor, mail_actor) == 1
        assert broker.queues[actor.queue_name].qsize() == 2
        assert NotificationDelivery.objects.get().pk == delivery.pk
    finally:
        broker.close()


def test_notification_runtime_closes_connections_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = MagicMock()
    monkeypatch.setattr("pyracast.background.actor.connections", SimpleNamespace(close_all=closed))
    failed = MagicMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(runtime, "process_notification", failed)
    with pytest.raises(RuntimeError, match="database unavailable"):
        runtime.deliver_notification(1)
    closed.assert_called_once()


@pytest.mark.skipif(
    os.environ.get("PYRACAST_LIVE_QUEUE") != "1",
    reason="requires a configured RabbitMQ server and PYRACAST_LIVE_QUEUE=1",
)
def test_rabbitmq_duplicate_reminder_sends_one_mail_without_provider_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()
    requests, jobs = RequestAttempt.objects.count(), CollectionJob.objects.count()
    monkeypatch.setattr(runtime, "datetime", SimpleNamespace(now=lambda _: SLOT))
    broker = make_broker()
    original = REMINDER.make_actor(broker)
    queue = f"test_reminders_{uuid.uuid4().hex}"
    actor = dramatiq.actor(broker=broker, actor_name=queue, queue_name=queue, max_retries=0)(
        original.fn
    )
    worker = Worker(broker, queues={queue}, worker_threads=1, worker_timeout=100)
    worker.start()
    try:
        actor.send(delivery.pk)
        actor.send(delivery.pk)
        broker.join(actor.queue_name, timeout=10000)
        # RabbitMQ join excludes unacknowledged messages; also await local worker completion.
        joined = Thread(target=worker.join, daemon=True)
        joined.start()
        joined.join(timeout=10)
        assert not joined.is_alive(), "RabbitMQ reminder worker did not finish within ten seconds."
    finally:
        worker.stop(timeout=10000)
        broker.close()
    delivery.refresh_from_db()
    assert delivery.status == "sent"
    assert delivery.attempts == 1
    assert len(mail.outbox) == 1
    assert mail.outbox[0].bcc == ["default@example.com"]
    assert RequestAttempt.objects.count() == requests
    assert CollectionJob.objects.count() == jobs
