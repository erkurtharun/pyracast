from datetime import UTC, datetime

import dramatiq
from django.db import close_old_connections, connections
from dramatiq import Actor, Broker

from pyracast.metering.jobs import pending_epias_ids, run_epias_job
from pyracast.metering.scheduler import enqueue_due_epias_jobs


def deliver_epias(job_id: int) -> None:
    close_old_connections()
    try:
        run_epias_job(job_id)
    finally:
        connections.close_all()


def make_epias_actor(broker: Broker) -> Actor[[int], None]:
    return dramatiq.actor(
        broker=broker, actor_name="collect_epias", queue_name="epias", max_retries=0
    )(deliver_epias)


def dispatch_epias(actor: Actor[[int], None]) -> int:
    now = datetime.now(UTC)
    enqueue_due_epias_jobs(now=now)
    identifiers = pending_epias_ids(now)
    for identifier in identifiers:
        actor.send(identifier)
    return len(identifiers)
