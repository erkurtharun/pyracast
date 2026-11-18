import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from typing import cast

import dramatiq
import pytest
from dramatiq import Worker
from dramatiq.brokers.stub import StubBroker

from pyracast.epias import EpiasClient, EpiasError
from pyracast.metering import client
from pyracast.metering.models import EpiasCollectionJob, EpiasSnapshot
from pyracast.metering.runtime import dispatch_epias, make_epias_actor
from pyracast.runtime import make_broker
from tests.support.domain import NOW
from tests.support.metering import FixtureClient, connected


def test_runtime_factory_requires_credentials_and_caches_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client._cached_client.cache_clear()
    monkeypatch.delenv("EPIAS_USERNAME", raising=False)
    monkeypatch.delenv("EPIAS_PASSWORD", raising=False)
    with pytest.raises(EpiasError, match="credentials_unavailable"):
        client.get_epias_client()
    monkeypatch.setenv("EPIAS_USERNAME", "fixture-user")
    monkeypatch.setenv("EPIAS_PASSWORD", "fixture-value")
    calls = []

    def create(*args: object, **kwargs: object) -> EpiasClient:
        calls.append(kwargs)
        return cast(EpiasClient, FixtureClient())

    monkeypatch.setattr(client, "EpiasClient", create)
    barrier = Barrier(2)

    def get() -> EpiasClient:
        barrier.wait(timeout=10)
        return client.get_epias_client()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: get(), range(2)))
        assert results[0] is results[1]
        assert len(calls) == 1
    finally:
        client._cached_client.cache_clear()


@pytest.mark.django_db(transaction=True)
def test_epias_queue_redelivery_archives_once_per_job(monkeypatch: pytest.MonkeyPatch) -> None:
    connected(monkeypatch)
    ready_ids = list(EpiasCollectionJob.objects.order_by("pk").values_list("pk", flat=True)[:2])
    deferred = EpiasCollectionJob.objects.exclude(pk__in=ready_ids)
    deferred.update(available_at=NOW + timedelta(days=1))
    monkeypatch.setattr("pyracast.metering.runtime.datetime", SimpleNamespace(now=lambda _: NOW))
    monkeypatch.setattr("pyracast.metering.jobs.get_epias_client", lambda: FixtureClient())
    broker = StubBroker()
    actor = make_epias_actor(broker)
    assert dispatch_epias(actor) == 2
    assert dispatch_epias(actor) == 2
    worker = Worker(broker, worker_threads=2, worker_timeout=100)
    worker.start()
    try:
        broker.join(actor.queue_name, timeout=10000, fail_fast=True)
        worker.join()
    finally:
        worker.stop()
        broker.close()
    assert set(EpiasSnapshot.objects.values_list("job_id", flat=True)) == set(ready_ids)
    assert EpiasSnapshot.objects.count() == len(ready_ids)
    completed = EpiasCollectionJob.objects.filter(pk__in=ready_ids)
    assert set(completed.values_list("status", flat=True)) == {"succeeded"}
    assert set(completed.values_list("attempts", flat=True)) == {1}
    assert set(deferred.values_list("status", flat=True)) == {"pending"}
    assert set(deferred.values_list("attempts", flat=True)) == {0}


@pytest.mark.skipif(
    os.environ.get("PYRACAST_LIVE_QUEUE") != "1",
    reason="requires a configured RabbitMQ server and PYRACAST_LIVE_QUEUE=1",
)
@pytest.mark.django_db(transaction=True)
def test_rabbitmq_delivers_epias_job_without_live_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected(monkeypatch)
    monkeypatch.setattr("pyracast.metering.jobs.get_epias_client", lambda: FixtureClient())
    job = EpiasCollectionJob.objects.order_by("pk").first()
    assert job is not None
    broker = make_broker()
    original = make_epias_actor(broker)
    queue = f"test_epias_{uuid.uuid4().hex}"
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
        assert job.attempts == 1
        assert EpiasSnapshot.objects.filter(job=job, complete=True).count() == 1
    finally:
        worker.stop()
        broker.close()
