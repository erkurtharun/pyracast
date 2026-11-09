from datetime import UTC, datetime

import dramatiq
from django.db import close_old_connections, connections
from dramatiq import Actor, Broker

from pyracast.plants.training import pending_calibration_ids, run_calibration


def deliver_calibration(run_id: int) -> None:
    close_old_connections()
    try:
        run_calibration(run_id)
    finally:
        connections.close_all()


def make_calibration_actor(broker: Broker) -> Actor[[int], None]:
    return dramatiq.actor(
        broker=broker, actor_name="calibrate_plant", queue_name="calibration", max_retries=0
    )(deliver_calibration)


def dispatch_calibrations(actor: Actor[[int], None]) -> int:
    identifiers = pending_calibration_ids(datetime.now(UTC))
    for identifier in identifiers:
        actor.send(identifier)
    return len(identifiers)
