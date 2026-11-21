import logging
from datetime import UTC, datetime
from functools import partial

import dramatiq
from django.db import close_old_connections, connections
from dramatiq import Actor, Broker

from pyracast.background.deadlines import time_limit_ms
from pyracast.background.failures import publish_after
from pyracast.metering.jobs import pending_epias_ids, run_epias_job
from pyracast.metering.scheduler import enqueue_due_epias_jobs

logger = logging.getLogger(__name__)


def deliver_epias(job_id: int) -> None:
    close_old_connections()
    try:
        run_epias_job(job_id)
    finally:
        connections.close_all()


def make_epias_actor(broker: Broker) -> Actor[[int], None]:
    return dramatiq.actor(
        broker=broker,
        actor_name="collect_epias",
        queue_name="epias",
        max_retries=0,
        time_limit=time_limit_ms("collect_epias"),
    )(deliver_epias)


def dispatch_epias(actor: Actor[[int], None]) -> int:
    now = datetime.now(UTC)
    return publish_after(
        partial(enqueue_due_epias_jobs, now=now),
        partial(pending_epias_ids, now),
        actor,
        logger=logger,
        phase="metering",
    )
