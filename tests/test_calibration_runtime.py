import os
import time
import uuid
from datetime import timedelta

import dramatiq
import pytest
from dramatiq import Worker
from dramatiq.brokers.stub import StubBroker

from pyracast.background.broker import make_broker
from pyracast.forecasts.models import RequestAttempt
from pyracast.plants.models import CalibrationRun
from pyracast.plants.operations import create_plant, request_calibration
from pyracast.plants.runtime import dispatch_calibrations, make_calibration_actor
from tests.support.domain import NOW, make_system


def calibration_request() -> CalibrationRun:
    system = make_system(approved=True)
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        name="Plant",
        dc_capacity_mw=130,
        export_limit_mw=100,
    )
    assert plant.current_configuration_id is not None
    return request_calibration(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        start_date=NOW.date() - timedelta(days=7),
        end_date=NOW.date() - timedelta(days=1),
    )


@pytest.mark.django_db(transaction=True)
def test_calibration_queue_redelivery_completes_once_without_provider_requests() -> None:
    run = calibration_request()
    requests = RequestAttempt.objects.count()
    broker = StubBroker()
    actor = make_calibration_actor(broker)
    assert dispatch_calibrations(actor) == 1
    assert dispatch_calibrations(actor) == 1
    worker = Worker(broker, worker_threads=1, worker_timeout=100)
    worker.start()
    try:
        broker.join(actor.queue_name, timeout=10000, fail_fast=True)
        worker.join()
    finally:
        worker.stop()
        broker.close()
    run.refresh_from_db()
    assert run.status == "insufficient"
    assert run.attempts == 1
    assert run.dataset_sha256
    assert RequestAttempt.objects.count() == requests


@pytest.mark.skipif(
    os.environ.get("PYRACAST_LIVE_QUEUE") != "1",
    reason="requires a configured RabbitMQ server and PYRACAST_LIVE_QUEUE=1",
)
@pytest.mark.django_db(transaction=True)
def test_rabbitmq_delivers_calibration_job() -> None:
    run = calibration_request()
    broker = make_broker()
    original = make_calibration_actor(broker)
    queue = f"test_calibration_{uuid.uuid4().hex}"
    actor = dramatiq.actor(broker=broker, actor_name=queue, queue_name=queue, max_retries=0)(
        original.fn
    )
    worker = Worker(broker, queues={queue}, worker_threads=1, worker_timeout=100)
    worker.start()
    try:
        actor.send(run.pk)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run.refresh_from_db()
            if run.status == "insufficient":
                break
            time.sleep(0.1)
        assert run.status == "insufficient"
        assert run.attempts == 1
    finally:
        worker.stop()
        broker.close()
