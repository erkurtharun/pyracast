import logging
from datetime import datetime, timedelta
from functools import partial
from itertools import chain
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction

from pyracast.accounts.models import Tenant
from pyracast.background.failures import BACK_OFF, Deferrals, isolate
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import CollectionJobStatus, ForecastSnapshot
from pyracast.local_time import local_slot
from pyracast.notifications.eligibility import binding_can_notify
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryError,
    NotificationDeliveryStatus,
    ReminderSchedule,
)
from pyracast.notifications.operations import recipient_limit, validate_recipient
from pyracast.notifications.recipients import effective_recipients, freeze_recipients
from pyracast.regions.models import Region, active_region

MAX_LATENESS = timedelta(hours=2)
PREPARATION_GRACE = timedelta(minutes=15)


logger = logging.getLogger(__name__)
_deferrals = Deferrals(BACK_OFF)


def enqueue_due(now: datetime) -> list[int]:
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    schedules = list(
        ReminderSchedule.objects.filter(active_region("region__"), enabled=True)
        .exclude(pk__in=_deferrals.active(now))
        .select_related("region__approved_binding__provider_account")
        .order_by("pk")
    )
    batches = isolate(
        schedules,
        partial(_enqueue_schedule, now=now),
        logger=logger,
        event="notification_schedule_failed",
        name=lambda schedule: f"schedule:{schedule.pk}",
        back_off=lambda schedule: _deferrals.defer(schedule.pk, now),
    )
    return list(chain.from_iterable(batches))


def recipient_refusal(recipients: list[str], limit: int) -> str:
    try:
        for address in recipients:
            validate_recipient(address)
    except ValidationError:
        return NotificationDeliveryError.INVALID_RECIPIENT
    if not recipients:
        return NotificationDeliveryError.NO_RECIPIENTS
    if len(recipients) > limit:
        return NotificationDeliveryError.RECIPIENT_LIMIT_EXCEEDED
    return ""


def slot_recipients(schedule: ReminderSchedule, limit: int) -> tuple[list[str], str]:
    recipients = effective_recipients(schedule)
    return recipients, recipient_refusal(recipients, limit)


def _enqueue_schedule(schedule: ReminderSchedule, now: datetime) -> list[int]:
    binding = schedule.region.approved_binding
    if binding is None or not binding_can_notify(binding):
        return []
    zone = ZoneInfo(schedule.region.timezone)
    today = now.astimezone(zone).date()
    slots = [
        (clock, slot)
        for day in (today - timedelta(days=1), today)
        for clock in schedule.times[: binding.provider_account.daily_quota]
        if (slot := local_slot(day, clock, zone)) is not None
        and schedule.effective_from <= slot <= now
    ]
    existing = dict(
        NotificationDelivery.objects.filter(
            schedule=schedule, scheduled_for__in=[slot for _, slot in slots]
        ).values_list("scheduled_for", "pk")
    )
    ids: list[int] = []
    for clock, slot in slots:
        delivery_id = existing.get(slot) or _create_slot(schedule, binding, clock, slot, now)
        if delivery_id is not None:
            ids.append(delivery_id)
    return ids


def _latest_snapshot(binding: SiteBinding, slot: datetime) -> ForecastSnapshot | None:
    """The forecast saved by the slot; a slot that is never prepared shows its age."""
    return (
        ForecastSnapshot.objects.filter(
            job__binding=binding,
            job__status=CollectionJobStatus.SUCCEEDED,
            source_kind="forecast",
            collected_at__lte=slot,
            forecastresult__published_at__lte=slot,
        )
        .order_by("-collected_at", "-pk")
        .first()
    )


def _create_slot(
    schedule: ReminderSchedule, binding: SiteBinding, clock: str, slot: datetime, now: datetime
) -> int | None:
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=schedule.region.tenant_id)
        limit = recipient_limit(lock=True)
        region = Region.objects.select_for_update().filter(pk=schedule.region_id).first()
        current = ReminderSchedule.objects.filter(pk=schedule.pk).first()
        if (
            region is None
            or current is None
            or not binding_can_notify(binding)
            or not current.enabled
            or current.effective_from > slot
            or clock not in current.times[: binding.provider_account.daily_quota]
        ):
            return None
        existing: int | None = (
            NotificationDelivery.objects.filter(schedule=current, scheduled_for=slot)
            .values_list("pk", flat=True)
            .first()
        )
        if existing is not None:
            return existing
        current.region = region
        recipients, reason = slot_recipients(current, limit)
        late = now - slot > MAX_LATENESS
        if reason:
            logger.info("notification_slot_skipped schedule_id=%s reason=%s", current.pk, reason)
        delivery = NotificationDelivery.objects.create(
            schedule=current,
            scheduled_for=slot,
            language=current.language,
            snapshot=None if reason else _latest_snapshot(binding, slot),
            binding=binding,
            preparation_deadline=slot + PREPARATION_GRACE,
            available_at=now,
            status=NotificationDeliveryStatus.SKIPPED
            if reason or late
            else NotificationDeliveryStatus.PENDING,
            last_error=reason or (NotificationDeliveryError.MISSED_SCHEDULE if late else ""),
        )
        if not reason:
            freeze_recipients(delivery, recipients)
        return delivery.pk
