import logging
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial

from dramatiq import Actor

from pyracast.background.actor import fresh_connections
from pyracast.background.failures import ItemsFailedError, publish_after, queue_full
from pyracast.notifications.jobs import pending_notification_ids, process_notification
from pyracast.notifications.outbox import end_expired_mail, publish_due_mail, send_outbound_mail
from pyracast.notifications.scheduler import enqueue_due

logger = logging.getLogger(__name__)


@fresh_connections
def deliver_notification(delivery_id: int) -> None:
    process_notification(delivery_id, datetime.now(UTC))


@fresh_connections
def deliver_outbound_mail(mail_id: int) -> None:
    send_outbound_mail(mail_id, datetime.now(UTC))


def publish_outbound_mail(actor: Actor[[int], None], now: datetime | None = None) -> int:
    timestamp = now or datetime.now(UTC)
    end_expired_mail(timestamp)
    if queue_full(actor, logger):
        return 0
    return publish_due_mail(actor.send, timestamp)


def dispatch_notifications(
    actor: Actor[[int], None], mail_actor: Actor[[int], None], now: datetime | None = None
) -> int:
    # Like the retention steps, item failures of the reminders do not skip the mail: they are
    # summed and raised after both steps ran; a step failing as a whole ends the phase.
    timestamp = now or datetime.now(UTC)
    steps: tuple[Callable[[], int], ...] = (
        partial(
            publish_after,
            partial(enqueue_due, timestamp),
            partial(pending_notification_ids, timestamp),
            actor,
            logger=logger,
            phase="notification",
        ),
        partial(publish_outbound_mail, mail_actor, timestamp),
    )
    published: list[int | None] = []
    failed = 0
    for step in steps:
        try:
            published.append(step())
        except ItemsFailedError as error:
            failed += error.count
            published.append(error.published)
    total = sum(count or 0 for count in published)
    if failed:
        summed = ItemsFailedError(failed)
        summed.published = None if None in published else total
        raise summed
    return total
