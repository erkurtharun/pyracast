from datetime import UTC, datetime

import dramatiq
from django.db import close_old_connections, connections
from dramatiq import Actor, Broker

from pyracast.notifications.jobs import pending_notification_ids, process_notification
from pyracast.notifications.scheduler import enqueue_due


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
    try:
        enqueue_due(timestamp)
    finally:
        close_old_connections()
        ids = pending_notification_ids(timestamp)
        for delivery_id in ids:
            actor.send(delivery_id)
    return len(ids)
