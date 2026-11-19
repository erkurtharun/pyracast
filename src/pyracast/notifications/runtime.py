import logging
from datetime import UTC, datetime
from functools import partial

import dramatiq
from django.db import close_old_connections, connections
from dramatiq import Actor, Broker

from pyracast.background.failures import publish_after
from pyracast.notifications.jobs import pending_notification_ids, process_notification
from pyracast.notifications.scheduler import enqueue_due

logger = logging.getLogger(__name__)


def deliver_notification(delivery_id: int) -> None:
    close_old_connections()
    try:
        process_notification(delivery_id, datetime.now(UTC))
    finally:
        connections.close_all()


def make_notification_actor(broker: Broker) -> Actor[[int], None]:
    return dramatiq.actor(
        broker=broker,
        actor_name="send_forecast_reminder",
        queue_name="notifications",
        max_retries=0,
    )(deliver_notification)


def dispatch_notifications(actor: Actor[[int], None], now: datetime | None = None) -> int:
    timestamp = now or datetime.now(UTC)
    return publish_after(
        partial(enqueue_due, timestamp),
        partial(pending_notification_ids, timestamp),
        actor,
        logger=logger,
        phase="notification",
    )
