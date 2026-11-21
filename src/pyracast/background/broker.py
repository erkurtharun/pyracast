from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core.checks import CheckMessage, Error
from django.core.exceptions import ImproperlyConfigured
from dramatiq.brokers.rabbitmq import RabbitmqBroker
from dramatiq.common import xq_name

from pyracast.background.deadlines import (
    BROKER_CALL_SECONDS,
    BROKER_HEARTBEAT_SECONDS,
    BROKER_SOCKET_SECONDS,
)
from pyracast.background.failures import BROKER_ERRORS, broker_read, missing_queue

LOCAL_BROKER_URL = "amqp://guest:guest@127.0.0.1:5672/%2F"
MISSING_URL = "Set PYRACAST_RABBITMQ_URL; only development falls back to a local broker."


def broker_url() -> str:
    return settings.PYRACAST_RABBITMQ_URL.strip() or (LOCAL_BROKER_URL if settings.DEBUG else "")


def check_broker_url(**_kwargs: Any) -> list[CheckMessage]:
    return [] if broker_url() else [Error(MISSING_URL, id="background.E001")]


def _bounded(url: str) -> str:
    parts = urlsplit(url)
    parameters = {
        "socket_timeout": str(BROKER_SOCKET_SECONDS),
        "stack_timeout": str(BROKER_CALL_SECONDS),
        "blocked_connection_timeout": str(BROKER_CALL_SECONDS),
        "heartbeat": str(BROKER_HEARTBEAT_SECONDS),
        "connection_attempts": "1",
        **dict(parse_qsl(parts.query, keep_blank_values=True)),
    }
    return urlunsplit(parts._replace(query=urlencode(parameters)))


def make_broker() -> RabbitmqBroker:
    configured = broker_url()
    if not configured:
        raise ImproperlyConfigured(MISSING_URL)
    # Semicolons separate failover nodes.
    urls = ";".join(_bounded(url) for url in configured.split(";"))
    return RabbitmqBroker(url=urls, confirm_delivery=True)


@dataclass(frozen=True)
class QueueState:
    """A work queue's counts and its dead-letter count; None where the broker has no such queue."""

    queue: str
    messages: int | None
    consumers: int | None
    dead_letters: int | None

    @property
    def missing(self) -> bool:
        return self.messages is None


def _declared(broker: RabbitmqBroker, queue: str) -> Any:
    # A passive declare reads the counts and fails instead of creating a missing queue.
    try:
        return broker.channel.queue_declare(queue=queue, passive=True).method
    except BROKER_ERRORS as error:
        if not missing_queue(error):
            raise
        # The broker closed only this channel; the next read opens another on the same connection.
        del broker.channel
        return None


def _queue_state(broker: RabbitmqBroker, queue: str) -> QueueState:
    main = _declared(broker, queue)
    dead = _declared(broker, xq_name(queue))
    return QueueState(
        queue,
        None if main is None else main.message_count,
        None if main is None else main.consumer_count,
        None if dead is None else dead.message_count,
    )


def queue_states(queues: Sequence[str]) -> tuple[QueueState, ...]:
    broker = make_broker()
    try:
        return broker_read(
            broker, lambda live: tuple(_queue_state(live, queue) for queue in queues)
        )
    finally:
        broker.close()
