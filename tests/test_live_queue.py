import os
import time

import pytest
from dramatiq import Worker

from pyracast import runtime
from pyracast.forecasts.models import CollectionJob, ForecastResult
from tests.test_domain import make_system


@pytest.mark.skipif(
    os.environ.get("PYRACAST_LIVE_QUEUE") != "1",
    reason="requires a configured RabbitMQ server and PYRACAST_LIVE_QUEUE=1",
)
@pytest.mark.django_db(transaction=True)
def test_rabbitmq_delivery_publishes_forecast() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    broker = runtime.make_broker()
    actor = runtime.make_actor(broker)
    worker = Worker(broker, worker_threads=1, worker_timeout=100)
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
