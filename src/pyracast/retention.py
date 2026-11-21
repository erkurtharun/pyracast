import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from pyracast.accounts.retention import (
    end_expired_sessions,
    purge_closed_invitations,
    reclaim_abandoned_signups,
)
from pyracast.notifications.outbox import purge_finished_mail
from pyracast.notifications.retention import trim_rendered_mail

logger = logging.getLogger(__name__)
# Rows one sweep removes or trims per dispatcher cycle, oldest first; the next cycle continues.
SWEEP_BATCH = 500


@dataclass(frozen=True)
class Retention:
    rows: str
    period: timedelta
    # Removes or trims at most `limit` rows that ended before the cutoff; returns how many.
    sweep: Callable[[datetime, int], int]

    def run(self, now: datetime) -> int:
        count = self.sweep(now - self.period, SWEEP_BATCH)
        if count:
            logger.info("retention_swept rows=%s count=%s", self.rows, count)
        return count


# How long each kind of row is kept after it ended: the owner's privacy decision, changed only
# with the owner.
RETENTION = (
    Retention("sessions", timedelta(0), end_expired_sessions),
    Retention("abandoned_signups", timedelta(days=7), reclaim_abandoned_signups),
    Retention("finished_mail", timedelta(days=7), purge_finished_mail),
    Retention("closed_invitations", timedelta(days=90), purge_closed_invitations),
    Retention("rendered_reminder_mail", timedelta(days=90), trim_rendered_mail),
)
