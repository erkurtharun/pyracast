import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import close_old_connections, transaction

from pyracast.accounts.language import user_language
from pyracast.accounts.models import Membership
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.notifications.eligibility import binding_can_notify
from pyracast.notifications.models import NotificationDelivery, ReminderPreference
from pyracast.notifications.operations import (
    effective_recipients,
    recipient_limit,
    region_recipient_count,
    validate_recipient,
)
from pyracast.notifications.schedule import local_slot
from pyracast.regions.models import Region

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
    for preference in ReminderPreference.objects.filter(
        enabled=True, region__deleted_at__isnull=True
    ).select_related("user", "region__approved_binding__provider_account"):
        try:
            ids.extend(_enqueue_preference(preference, now))
        except Exception as error:
            failures += 1
            logger.error(
                "notification_schedule_failed preference_id=%s error_type=%s",
                preference.pk,
                type(error).__name__,
            )
            close_old_connections()
    if failures:
        raise NotificationSchedulingError(
            f"{failures} email reminder preference(s) could not be scheduled."
        )
    return ids


def _enqueue_preference(preference: ReminderPreference, now: datetime) -> list[int]:
    ids: list[int] = []
    region, binding = preference.region, preference.region.approved_binding
    if (
        not preference.user.is_active
        or binding is None
        or not binding_can_notify(binding)
        or not Membership.objects.filter(user=preference.user, tenant_id=region.tenant_id).exists()
    ):
        return []
    zone = ZoneInfo(region.timezone)
    today = now.astimezone(zone).date()
    for day in (today - timedelta(days=1), today):
        for clock in preference.times[: binding.provider_account.daily_quota]:
            slot = local_slot(day, clock, zone)
            if slot is None or slot > now or slot < preference.effective_from:
                continue
            with transaction.atomic():
                limit = recipient_limit(lock=True)
                locked_region = Region.objects.select_for_update().filter(pk=region.pk).first()
                current = (
                    ReminderPreference.objects.select_related("user")
                    .filter(pk=preference.pk)
                    .first()
                )
                if (
                    locked_region is None
                    or current is None
                    or not binding_can_notify(binding)
                    or not current.enabled
                    or current.effective_from > slot
                    or clock not in current.times[: binding.provider_account.daily_quota]
                ):
                    continue
                recipients = effective_recipients(current)
                for address in recipients:
                    validate_recipient(address)
                if region_recipient_count(locked_region) > limit:
                    continue
                existing_ids = list(
                    NotificationDelivery.objects.filter(preference=current, scheduled_for=slot)
                    .order_by("pk")
                    .values_list("pk", flat=True)
                )
                if existing_ids:
                    ids.extend(existing_ids)
                    continue
                snapshot = (
                    ForecastSnapshot.objects.filter(
                        job__binding=binding,
                        job__status="succeeded",
                        source_kind="forecast",
                        collected_at__lte=slot,
                        forecastresult__published_at__lte=slot,
                    )
                    .order_by("-collected_at", "-pk")
                    .first()
                )
                language = user_language(current.user) or "en"
                for recipient in recipients:
                    delivery, _ = NotificationDelivery.objects.get_or_create(
                        preference=current,
                        scheduled_for=slot,
                        recipient=recipient,
                        defaults={
                            "language": language,
                            "snapshot": snapshot,
                            "binding": binding,
                            "preparation_deadline": slot + PREPARATION_GRACE,
                            "available_at": now,
                            "status": "skipped" if now - slot > MAX_LATENESS else "pending",
                            "last_error": "missed_schedule" if now - slot > MAX_LATENESS else "",
                        },
                    )
                    ids.append(delivery.pk)
    return ids
