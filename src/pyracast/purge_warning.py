import logging
from datetime import datetime, timedelta
from functools import partial
from typing import cast

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from pyracast.accounts.event_mail import reachable_members, recipient_language
from pyracast.accounts.mail_intents import enqueue_mail
from pyracast.accounts.models import OutboundMailKind, WorkspaceRole
from pyracast.background.failures import BACK_OFF, Deferrals, isolate
from pyracast.regions.models import Region
from pyracast.trash import RETENTION, trashed_within_retention

PURGE_WARNING_LEAD = timedelta(days=7)
WARNING_BATCH_SIZE = 100

logger = logging.getLogger(__name__)
_deferrals = Deferrals(BACK_OFF)


def _warnable(now: datetime) -> Q:
    """Trashed regions inside the week before their purge that were never warned; once the
    purge is due the purge runs, never a late warning."""
    return trashed_within_retention(now, PURGE_WARNING_LEAD) & Q(
        purge_started_at__isnull=True,
        purge_warning_sent_at__isnull=True,
    )


def warn_before_purge(now: datetime) -> int:
    """Writes one warning per workspace administrator for each region due a warning, in the
    transaction that marks the region warned. The purge never reads the mark."""
    if now.utcoffset() is None:
        raise ValidationError("An aware timestamp is required.")
    due = list(
        Region.objects.filter(_warnable(now))
        .exclude(pk__in=_deferrals.active(now))
        .order_by("deleted_at", "pk")
        .values_list("pk", flat=True)[:WARNING_BATCH_SIZE]
    )
    warned = isolate(
        due,
        partial(_warn, now=now),
        logger=logger,
        event="purge_warning_failed",
        name=lambda region_id: f"region:{region_id}",
        back_off=lambda region_id: _deferrals.defer(region_id, now),
    )
    return sum(warned)


def _warn(region_id: int, now: datetime) -> bool:
    with transaction.atomic():
        region = Region.objects.select_for_update().filter(_warnable(now), pk=region_id).first()
        if region is None:
            return False
        region.purge_warning_sent_at = now
        region.save(update_fields=("purge_warning_sent_at",))
        for membership in reachable_members(region.tenant_id).filter(role=WorkspaceRole.ADMIN):
            enqueue_mail(
                OutboundMailKind.PURGE_WARNING,
                membership.user.email,
                language=recipient_language(membership.user),
                # The warning ends when the purge is due; the mail states this time.
                expires_at=cast(datetime, region.deleted_at) + RETENTION,
                now=now,
                context={"subject_id": str(region.pk)},
            )
        return True
