import math
import secrets
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

_MAX_RETRY_SECONDS = (datetime.max - datetime.min) // timedelta(seconds=1)


def retry_after_seconds(value: str | None) -> int | None:
    if value is None or len(value) > 128:
        return None
    try:
        seconds = (
            int(value)
            if value.isascii() and value.isdecimal()
            else math.ceil((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        )
    except ValueError, TypeError, OverflowError:
        return None
    return max(0, min(_MAX_RETRY_SECONDS, seconds))


def retryable_status(status: int) -> bool:
    return status in {408, 425, 429} or (500 <= status < 600 and status not in {501, 505})


def retry_delay(
    attempts: int, retry_after: int | None = None, *, minimum_seconds: int = 0
) -> timedelta:
    base = max(
        30 * 2 ** min(attempts, 6), minimum_seconds, min(retry_after or 0, _MAX_RETRY_SECONDS)
    )
    return timedelta(seconds=base + secrets.randbelow(31))


def next_retry_at(
    now: datetime, attempts: int, retry_after: int | None = None, *, minimum_seconds: int = 0
) -> datetime:
    try:
        return now.astimezone(UTC) + retry_delay(
            attempts, retry_after, minimum_seconds=minimum_seconds
        )
    except OverflowError:
        return datetime.max.replace(tzinfo=UTC)
