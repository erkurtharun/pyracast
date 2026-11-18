import logging
import traceback
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from django.db import close_old_connections
from dramatiq import Actor

_PACKAGE = Path(__file__).resolve().parents[1]
BACK_OFF = timedelta(minutes=15)


class ItemsFailedError(Exception):
    def __init__(self, count: int) -> None:
        super().__init__(f"{count} item(s) failed")
        self.count = count
        self.published: int | None = None


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
    """Run each item outside a transaction; a failing item is logged and backed off.

    Raises ItemsFailedError after the last item when any item failed.
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


def _send(identifiers: Sequence[int], actor: Actor[[int], None]) -> int:
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
            published: int | None = _send(pending(), actor)
        except Exception as publish_error:
            log_failure(logger, f"{phase}_publish_failed", publish_error)
            published = None
        if isinstance(error, ItemsFailedError):
            error.published = published
        raise
    return _send(pending(), actor)
