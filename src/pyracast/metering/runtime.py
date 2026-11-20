import logging
from datetime import UTC, datetime
from functools import partial

from dramatiq import Actor

from pyracast.background.actor import fresh_connections
from pyracast.background.failures import publish_after
from pyracast.metering.jobs import pending_epias_ids, run_epias_job
from pyracast.metering.scheduler import enqueue_due_epias_jobs

logger = logging.getLogger(__name__)


@fresh_connections
def deliver_epias(job_id: int) -> None:
    run_epias_job(job_id)


def dispatch_epias(actor: Actor[[int], None]) -> int:
    now = datetime.now(UTC)
    return publish_after(
        partial(enqueue_due_epias_jobs, now=now),
        partial(pending_epias_ids, now),
        actor,
        logger=logger,
        phase="metering",
    )
