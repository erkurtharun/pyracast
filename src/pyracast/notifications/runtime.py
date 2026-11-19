import logging
from datetime import UTC, datetime
from functools import partial

import dramatiq
from django.db import close_old_connections, connections
from dramatiq import Actor, Broker

from pyracast.background.failures import publish_after
from pyracast.notifications.jobs import pending_notification_ids, process_notification
from pyracast.notifications.outbox import end_expired_mail, publish_due_mail, send_outbound_mail
from pyracast.notifications.scheduler import enqueue_due

logger = logging.getLogger(__name__)


def deliver_notification(delivery_id: int) -> None:
    close_old_connections()
    try:
        process_notification(delivery_id, datetime.now(UTC))
    finally:
        connections.close_all()


def deliver_outbound_mail(mail_id: int) -> None:
    close_old_connections()
    try:
        send_outbound_mail(mail_id, datetime.now(UTC))
    finally:
        connections.close_all()


def make_notification_actor(broker: Broker) -> Actor[[int], None]:
    return dramatiq.actor(
        broker=broker,
        actor_name="send_forecast_reminder",
        queue_name="notifications",
        max_retries=0,
    )(deliver_notification)


def make_outbound_mail_actor(broker: Broker) -> Actor[[int], None]:
    return dramatiq.actor(
        broker=broker,
        actor_name="send_outbound_mail",
        queue_name="notifications",
        max_retries=0,
    )(deliver_outbound_mail)


def publish_outbound_mail(actor: Actor[[int], None], now: datetime | None = None) -> int:
    timestamp = now or datetime.now(UTC)
    end_expired_mail(timestamp)
    return publish_due_mail(actor.send, timestamp)


def dispatch_notifications(
    actor: Actor[[int], None], mail_actor: Actor[[int], None], now: datetime | None = None
) -> int:
    timestamp = now or datetime.now(UTC)
    published = publish_after(
        partial(enqueue_due, timestamp),
        partial(pending_notification_ids, timestamp),
        actor,
        logger=logger,
        phase="notification",
    )
    return published + publish_outbound_mail(mail_actor, timestamp)
