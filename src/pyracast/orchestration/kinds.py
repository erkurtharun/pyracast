from collections.abc import Callable
from dataclasses import dataclass

from dramatiq import Actor, Broker

from pyracast.background.actor import job_actor
from pyracast.forecasts.runtime import deliver
from pyracast.metering.runtime import deliver_epias
from pyracast.notifications.runtime import deliver_notification, deliver_outbound_mail
from pyracast.plants.runtime import deliver_calibration


@dataclass(frozen=True)
class JobKind:
    # The actor name is also the job's key in background.deadlines.ACTORS.
    actor: str
    queue: str
    deliver: Callable[[int], None]

    def make_actor(self, broker: Broker) -> Actor[[int], None]:
        return job_actor(broker, self.actor, self.queue, self.deliver)


MAIL_QUEUE = "notifications"
COLLECTION = JobKind("collect_forecast", "default", deliver)
CALIBRATION = JobKind("calibrate_plant", "calibration", deliver_calibration)
REMINDER = JobKind("send_forecast_reminder", MAIL_QUEUE, deliver_notification)
OUTBOUND_MAIL = JobKind("send_outbound_mail", MAIL_QUEUE, deliver_outbound_mail)
METERING = JobKind("collect_epias", "epias", deliver_epias)
KINDS = (COLLECTION, CALIBRATION, REMINDER, OUTBOUND_MAIL, METERING)
QUEUES = tuple(dict.fromkeys(kind.queue for kind in KINDS))
# The mail queue has a worker service of its own (deploy/compose.yaml).
WORKER_QUEUES = tuple(queue for queue in QUEUES if queue != MAIL_QUEUE)


def register_actors(broker: Broker) -> dict[str, Actor[[int], None]]:
    return {kind.actor: kind.make_actor(broker) for kind in KINDS}
