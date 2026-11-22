import logging
from collections.abc import Callable

from django.db import DatabaseError
from psycopg.errors import DeadlockDetected, LockNotAvailable

logger = logging.getLogger(__name__)


def left_unclaimed(error: DatabaseError, kind: str, job_id: int) -> bool:
    """A claim that waited out the lock timeout or lost a deadlock rolled back its transaction and
    started nothing; like a job not yet claimable, its owner publishes it again."""
    if not isinstance(error.__cause__, LockNotAvailable | DeadlockDetected):
        return False
    logger.warning(
        "job_claim_contended kind=%s job_id=%s error_type=%s",
        kind,
        job_id,
        type(error.__cause__).__name__,
    )
    return True


def claim_unless_contended[T](kind: str, job_id: int, claim: Callable[[], T]) -> T | None:
    """`claim` runs the claim's whole transaction; any other failure propagates."""
    try:
        return claim()
    except DatabaseError as error:
        if not left_unclaimed(error, kind, job_id):
            raise
    return None
