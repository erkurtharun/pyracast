import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import close_old_connections, transaction

from pyracast.accounts.models import Tenant
from pyracast.forecasts.models import CollectionJobStatus, ForecastSnapshot
from pyracast.notifications.eligibility import binding_can_notify
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryError,
    NotificationDeliveryStatus,
    ReminderSchedule,
)
from pyracast.notifications.operations import (
    effective_recipients,
    recipient_limit,
    validate_recipient,
)
from pyracast.notifications.recipients import pin_recipient_memberships
from pyracast.notifications.schedule import local_slot
from pyracast.regions.models import Region, active_region

MAX_LATENESS = timedelta(hours=2)
PREPARATION_GRACE = timedelta(minutes=15)


logger = logging.getLogger(__name__)


class NotificationSchedulingError(RuntimeError):
    pass


def enqueue_due(now: datetime) -> list[int]:
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    ids: list[int] = []
    failures = 0
    for schedule in ReminderSchedule.objects.filter(
        active_region("region__"), enabled=True
    ).select_related("region__approved_binding__provider_account"):
        try:
            ids.extend(_enqueue_schedule(schedule, now))
        except Exception as error:
            failures += 1
            logger.error(
                "notification_schedule_failed schedule_id=%s error_type=%s",
                schedule.pk,
                type(error).__name__,
            )
            close_old_connections()
    if failures:
        raise NotificationSchedulingError(
            f"{failures} email reminder schedule(s) could not be scheduled."
        )
    return ids


def slot_recipients(schedule: ReminderSchedule, limit: int) -> tuple[list[str], str]:
    try:
        recipients = effective_recipients(schedule)
        for address in recipients:
            validate_recipient(address)
    except ValidationError:
        return [], NotificationDeliveryError.INVALID_RECIPIENT
    if not recipients:
        return [], NotificationDeliveryError.NO_RECIPIENTS
    if len(recipients) > limit:
        return [], NotificationDeliveryError.RECIPIENT_LIMIT_EXCEEDED
    return recipients, ""


def _enqueue_schedule(schedule: ReminderSchedule, now: datetime) -> list[int]:
    ids: list[int] = []
    region, binding = schedule.region, schedule.region.approved_binding
    if binding is None or not binding_can_notify(binding):
        return []
    zone = ZoneInfo(region.timezone)
    today = now.astimezone(zone).date()
    for day in (today - timedelta(days=1), today):
        for clock in schedule.times[: binding.provider_account.daily_quota]:
            slot = local_slot(day, clock, zone)
            if slot is None or slot > now or slot < schedule.effective_from:
                continue
            with transaction.atomic():
                Tenant.objects.select_for_update().get(pk=region.tenant_id)
                limit = recipient_limit(lock=True)
                locked_region = Region.objects.select_for_update().filter(pk=region.pk).first()
                current = ReminderSchedule.objects.filter(pk=schedule.pk).first()
                if (
                    locked_region is None
                    or current is None
                    or not binding_can_notify(binding)
                    or not current.enabled
                    or current.effective_from > slot
                    or clock not in current.times[: binding.provider_account.daily_quota]
                ):
                    continue
                existing_ids = list(
                    NotificationDelivery.objects.filter(schedule=current, scheduled_for=slot)
                    .order_by("pk")
                    .values_list("pk", flat=True)
                )
                if existing_ids:
                    ids.extend(existing_ids)
                    continue
                recipients, reason = slot_recipients(current, limit)
                if reason:
                    logger.info(
                        "notification_slot_skipped schedule_id=%s reason=%s", current.pk, reason
                    )
                    paused = NotificationDelivery.objects.create(
                        schedule=current,
                        scheduled_for=slot,
                        recipients=[],
                        recipient_memberships={},
                        language=current.language,
                        binding=binding,
                        preparation_deadline=slot + PREPARATION_GRACE,
                        available_at=now,
                        status=NotificationDeliveryStatus.SKIPPED,
                        last_error=reason,
                    )
                    ids.append(paused.pk)
                    continue
                snapshot = (
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
                delivery = NotificationDelivery.objects.create(
                    schedule=current,
                    scheduled_for=slot,
                    recipients=recipients,
                    recipient_memberships=pin_recipient_memberships(current, recipients),
                    language=current.language,
                    snapshot=snapshot,
                    binding=binding,
                    preparation_deadline=slot + PREPARATION_GRACE,
                    available_at=now,
                    status=NotificationDeliveryStatus.SKIPPED
                    if now - slot > MAX_LATENESS
                    else NotificationDeliveryStatus.PENDING,
                    last_error=NotificationDeliveryError.MISSED_SCHEDULE
                    if now - slot > MAX_LATENESS
                    else "",
                )
                ids.append(delivery.pk)
    return ids
