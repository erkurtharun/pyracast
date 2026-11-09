import os
from datetime import UTC, datetime

import dramatiq
from django.db import close_old_connections, connections
from dramatiq import Actor, Broker
from dramatiq.brokers.rabbitmq import RabbitmqBroker

from pyracast.calculation import calculate
from pyracast.forecasts.jobs import collect, pending_job_ids
from pyracast.forecasts.models import CollectionJob
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.providers import adapter_for


def deliver(job_id: int) -> None:
    close_old_connections()
    try:
        job = CollectionJob.objects.select_related("binding__provider_account").get(pk=job_id)
        collect(job_id, adapter_for(job.binding), calculate, datetime.now(UTC))
    finally:
        connections.close_all()


def make_actor(broker: Broker) -> Actor[[int], None]:
    return dramatiq.actor(broker=broker, actor_name="collect_forecast", max_retries=0)(deliver)


def make_broker() -> RabbitmqBroker:
    return RabbitmqBroker(
        url=os.environ.get("PYRACAST_RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/%2F"),
        confirm_delivery=True,
    )


def dispatch(actor: Actor[[int], None]) -> int:
    now = datetime.now(UTC)
    enqueue_due(now)
    job_ids = pending_job_ids(now)
    for job_id in job_ids:
        actor.send(job_id)
    return len(job_ids)
