import os
import time
import uuid
from datetime import timedelta
from threading import Thread
from types import SimpleNamespace

import dramatiq
import pytest
from dramatiq import Worker

from pyracast.background.broker import make_broker
from pyracast.forecasts import runtime
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.orchestration.kinds import COLLECTION
from tests.support.domain import NOW, make_system


@pytest.mark.skipif(
    os.environ.get("PYRACAST_LIVE_QUEUE") != "1",
    reason="requires a configured RabbitMQ server and PYRACAST_LIVE_QUEUE=1",
)
@pytest.mark.django_db(transaction=True)
def test_rabbitmq_delivery_publishes_forecast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "datetime", SimpleNamespace(now=lambda _: NOW))
    make_system(approved=True)
    job = CollectionJob.objects.get()
    broker = make_broker()
    original = COLLECTION.make_actor(broker)
    queue = f"test_forecast_{uuid.uuid4().hex}"
    actor = dramatiq.actor(broker=broker, actor_name=queue, queue_name=queue, max_retries=0)(
        original.fn
    )
    worker = Worker(broker, queues={queue}, worker_threads=1, worker_timeout=100)
    worker.start()
    try:
        actor.send(job.pk)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job.refresh_from_db()
            if job.status == "succeeded":
                break
            time.sleep(0.1)
        assert job.status == "succeeded"
        assert ForecastResult.objects.filter(snapshot__job=job).count() == 1
    finally:
        worker.stop()
        broker.close()


@pytest.mark.skipif(
    os.environ.get("PYRACAST_LIVE_QUEUE") != "1",
    reason="requires a configured RabbitMQ server and PYRACAST_LIVE_QUEUE=1",
)
@pytest.mark.django_db(transaction=True)
def test_rabbitmq_actuals_redelivery_archives_once_with_one_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "datetime", SimpleNamespace(now=lambda _: NOW))
    system = make_system(approved=True)
    job = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="estimated_actuals",
        scheduled_for=NOW,
        target_date=NOW.date() - timedelta(days=1),
        available_at=NOW,
    )
    requests = RequestAttempt.objects.count()
    broker = make_broker()
    original = COLLECTION.make_actor(broker)
    queue = f"test_actuals_{uuid.uuid4().hex}"
    actor = dramatiq.actor(broker=broker, actor_name=queue, queue_name=queue, max_retries=0)(
        original.fn
    )
    worker = Worker(broker, queues={queue}, worker_threads=1, worker_timeout=100)
    worker.start()
    try:
        actor.send(job.pk)
        actor.send(job.pk)
        broker.join(actor.queue_name, timeout=10000)
        joined = Thread(target=worker.join, daemon=True)
        joined.start()
        joined.join(timeout=10)
        assert not joined.is_alive(), "RabbitMQ actuals worker did not finish within ten seconds."
    finally:
        worker.stop(timeout=10000)
        broker.close()
    job.refresh_from_db()
    assert job.status == "succeeded"
    assert job.attempts == 1
    snapshot = ForecastSnapshot.objects.get(job=job)
    assert snapshot.source_kind == "estimated_actuals"
    assert snapshot.available_at is not None
    assert snapshot.coverage_start is not None
    assert snapshot.coverage_end is not None
    assert len(snapshot.normalized_intervals) == 336
    assert snapshot.raw_payload["synthetic"] is True
    assert not ForecastResult.objects.filter(snapshot__job=job).exists()
    attempt = RequestAttempt.objects.get(job=job)
    assert attempt.purpose == "estimated_actuals"
    assert attempt.outcome == "succeeded"
    assert RequestAttempt.objects.count() == requests + 1
