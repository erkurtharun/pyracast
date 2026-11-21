import logging
from datetime import UTC, datetime
from functools import partial

import dramatiq
from django.db import close_old_connections, connections
from dramatiq import Actor, Broker

from pyracast.background.deadlines import time_limit_ms
from pyracast.background.failures import ItemsFailedError, publish_after
from pyracast.plants.automation import prepare_due_calibrations
from pyracast.plants.preparation import prepare_requested_calibrations
from pyracast.plants.training import pending_calibration_ids, run_calibration

logger = logging.getLogger(__name__)


def deliver_calibration(run_id: int) -> None:
    close_old_connections()
    try:
        run_calibration(run_id)
    finally:
        connections.close_all()


def make_calibration_actor(broker: Broker) -> Actor[[int], None]:
    return dramatiq.actor(
        broker=broker,
        actor_name="calibrate_plant",
        queue_name="calibration",
        max_retries=0,
        time_limit=time_limit_ms("calibrate_plant"),
    )(deliver_calibration)


def _schedule(now: datetime) -> None:
    failed = 0
    for step in (prepare_due_calibrations, prepare_requested_calibrations):
        try:
            step(now)
        except ItemsFailedError as error:
            failed += error.count
    if failed:
        raise ItemsFailedError(failed)


def dispatch_calibrations(actor: Actor[[int], None]) -> int:
    now = datetime.now(UTC)
    return publish_after(
        partial(_schedule, now),
        partial(pending_calibration_ids, now),
        actor,
        logger=logger,
        phase="calibration",
    )
