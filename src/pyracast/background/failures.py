import logging
import traceback
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from django.db import close_old_connections
from dramatiq import Actor
from dramatiq.brokers.rabbitmq import RabbitmqBroker
from dramatiq.brokers.stub import StubBroker
from pika.exceptions import (  # type: ignore[import-untyped]
    AMQPChannelError,
    AMQPConnectionError,
)

from pyracast.background.deadlines import QUEUE_DEPTH_LIMIT

_PACKAGE = Path(__file__).resolve().parents[1]
BACK_OFF = timedelta(minutes=15)
BROKER_ERRORS = (AMQPConnectionError, AMQPChannelError)


class ItemsFailedError(Exception):
    def __init__(self, count: int) -> None:
        super().__init__(f"{count} item(s) failed")
        self.count = count
        self.published: int | None = None


@dataclass(frozen=True)
class PhaseOutcome:
    published: int | None = None
    # A bounded failure code (at most 100 characters); empty when the phase succeeded.
    error: str = ""


def _chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and all(current is not seen for seen in chain):
        chain.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return chain


def failure_origin(error: BaseException) -> str:
    for exception in reversed(_chain(error)):
        for frame in reversed(traceback.extract_tb(exception.__traceback__)):
            path = Path(frame.filename).resolve()
            if path.is_relative_to(_PACKAGE):
                module = ".".join(path.relative_to(_PACKAGE.parent).with_suffix("").parts)
                return f"{module}:{frame.lineno}:{frame.name}"
    return "unknown"


def log_failure(logger: logging.Logger, event: str, error: BaseException, **fields: object) -> None:
    """Never logs str(error) or the traceback: provider errors and URLs can carry secrets."""
    names = "".join(f" {name}=%s" for name in fields)
    logger.error(
        f"{event}{names} error_type=%s origin=%s",
        *fields.values(),
        type(error).__name__,
        failure_origin(error),
    )


def isolate[T, R](
    items: Iterable[T],
    operation: Callable[[T], R],
    *,
    logger: logging.Logger,
    event: str,
    name: Callable[[T], str],
    back_off: Callable[[T], object],
) -> list[R]:
    """Log and back off a failing item, then go on; raise ItemsFailedError after the last one.

    The caller runs this outside any transaction and gives each item its own, so a failure
    rolls back only that item.
    """
    results: list[R] = []
    failed = 0
    for item in items:
        try:
            results.append(operation(item))
        except Exception as error:
            failed += 1
            log_failure(logger, event, error, item=name(item))
            close_old_connections()
            try:
                back_off(item)
            except Exception as back_off_error:
                log_failure(logger, "back_off_failed", back_off_error, item=name(item))
                close_old_connections()
    if failed:
        raise ItemsFailedError(failed)
    return results


class Deferrals:
    """Process-local back-off for items without a durable due time.

    Holds only the items that failed within the last delay.
    """

    def __init__(self, delay: timedelta) -> None:
        self._delay = delay
        self._until: dict[int, datetime] = {}

    def active(self, now: datetime) -> list[int]:
        self._until = {key: until for key, until in self._until.items() if until > now}
        return list(self._until)

    def defer(self, key: int, now: datetime) -> None:
        self._until[key] = now + self._delay


def broker_read[T](broker: RabbitmqBroker, read: Callable[[RabbitmqBroker], T]) -> T:
    # Dramatiq drops a dead connection only inside enqueue and queue declaration, not here, so a
    # lost connection or channel is dropped before a second attempt on a fresh one.
    try:
        return read(broker)
    except BROKER_ERRORS:
        del broker.connection
    try:
        return read(broker)
    except BROKER_ERRORS:
        del broker.connection
        raise


def _rabbitmq_depth(broker: RabbitmqBroker, queue_name: str) -> int:
    return broker_read(broker, lambda live: int(live.get_queue_message_counts(queue_name)[0]))


def queue_depth(actor: Actor[[int], None]) -> int | None:
    broker = actor.broker
    if isinstance(broker, RabbitmqBroker):
        return _rabbitmq_depth(broker, actor.queue_name)
    if isinstance(broker, StubBroker):
        return int(broker.queues[actor.queue_name].qsize())
    return None


def broker_answers(actor: Actor[[int], None], logger: logging.Logger) -> bool:
    try:
        queue_depth(actor)
    except BROKER_ERRORS as error:
        log_failure(logger, "broker_unreachable", error, queue=actor.queue_name)
        return False
    return True


def queue_full(actor: Actor[[int], None], logger: logging.Logger) -> bool:
    """Ids stay in the database, so a skipped publication loses nothing while workers are down.

    An unreadable depth publishes anyway: the send reconnects or fails the phase, and one round
    adds at most one batch."""
    try:
        depth = queue_depth(actor)
    except BROKER_ERRORS as error:
        log_failure(logger, "queue_depth_unavailable", error, queue=actor.queue_name)
        return False
    if depth is None or depth < QUEUE_DEPTH_LIMIT:
        return False
    logger.warning("publication_skipped queue=%s depth=%s", actor.queue_name, depth)
    return True


def _send(
    pending: Callable[[], Sequence[int]], actor: Actor[[int], None], logger: logging.Logger
) -> int:
    if queue_full(actor, logger):
        return 0
    identifiers = pending()
    for identifier in identifiers:
        actor.send(identifier)
    return len(identifiers)


def publish_after(
    schedule: Callable[[], object],
    pending: Callable[[], Sequence[int]],
    actor: Actor[[int], None],
    *,
    logger: logging.Logger,
    phase: str,
) -> int:
    """Publish the pending ids even when scheduling failed, then re-raise its failure."""
    try:
        schedule()
    except Exception as error:
        close_old_connections()
        try:
            published: int | None = _send(pending, actor, logger)
        except Exception as publish_error:
            log_failure(logger, f"{phase}_publish_failed", publish_error)
            published = None
        if isinstance(error, ItemsFailedError):
            error.published = published
        raise
    return _send(pending, actor, logger)
