import logging
from datetime import UTC, datetime
from functools import partial

from dramatiq import Actor

from pyracast.background.actor import fresh_connections
from pyracast.background.failures import ItemsFailedError, publish_after
from pyracast.plants.automation import prepare_due_calibrations
from pyracast.plants.preparation import prepare_requested_calibrations
from pyracast.plants.training import pending_calibration_ids, run_calibration

logger = logging.getLogger(__name__)


@fresh_connections
def deliver_calibration(run_id: int) -> None:
    run_calibration(run_id)


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
