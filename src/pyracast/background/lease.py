import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Protocol

from django.db.models import Q

# The job tables hold both lease columns together, and only in a leased status (db.lease_check).
RELEASED: Mapping[str, None] = MappingProxyType({"lease_token": None, "lease_expires_at": None})


class Leased(Protocol):
    status: str
    attempts: int
    lease_token: uuid.UUID | None
    lease_expires_at: datetime | None


def expired(row: Leased, now: datetime) -> bool:
    return row.lease_expires_at is None or row.lease_expires_at <= now


def held(row: Leased, status: str, now: datetime) -> bool:
    """Another worker runs the job: it may not be claimed before the lease ends."""
    return row.status == status and not expired(row, now)


def carries(row: Leased, token: uuid.UUID, status: str) -> bool:
    """The row is still in the lease this token took, whether or not the lease has ended."""
    return row.status == status and row.lease_token == token


def owns(row: Leased, token: uuid.UUID, status: str, now: datetime) -> bool:
    return carries(row, token, status) and not expired(row, now)


def take(row: Leased, status: str, now: datetime, duration: timedelta) -> uuid.UUID:
    token = uuid.uuid4()
    row.status = status
    row.attempts += 1
    row.lease_token = token
    row.lease_expires_at = now + duration
    return token


def release(row: Leased) -> None:
    row.lease_token = None
    row.lease_expires_at = None


def claimable(status: str, now: datetime) -> Q:
    """Rows in the leased status whose lease has ended; another worker may claim them."""
    return Q(status=status, lease_expires_at__lte=now)


def holding(status: str, now: datetime) -> Q:
    return Q(status=status, lease_expires_at__gt=now)
