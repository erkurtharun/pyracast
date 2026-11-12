from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction

from pyracast.accounts.models import Membership
from pyracast.forecasts.jobs import binding_is_current
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.notifications.models import NotificationDelivery, ReminderPreference
from pyracast.notifications.operations import (
    effective_recipients,
    recipient_limit,
    region_recipient_count,
)
from pyracast.notifications.schedule import local_slot
from pyracast.regions.models import Region

MAX_LATENESS = timedelta(hours=2)


def enqueue_due(now: datetime) -> list[int]:
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    ids: list[int] = []
    for preference in ReminderPreference.objects.filter(
        enabled=True, region__deleted_at__isnull=True
    ).select_related("user", "region__approved_binding__provider_account"):
        region, binding = preference.region, preference.region.approved_binding
        if (
            not preference.user.is_active
            or binding is None
            or not binding_is_current(binding)
            or not Membership.objects.filter(
                user=preference.user, tenant_id=region.tenant_id
            ).exists()
        ):
            continue
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
                        or not binding_is_current(binding)
                        or not current.enabled
                        or current.effective_from > slot
                        or clock not in current.times[: binding.provider_account.daily_quota]
                    ):
                        continue
                    recipients = effective_recipients(current)
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
                    for recipient in recipients:
                        delivery, _ = NotificationDelivery.objects.get_or_create(
                            preference=current,
                            scheduled_for=slot,
                            recipient=recipient,
                            defaults={
                                "snapshot": snapshot,
                                "available_at": now,
                                "status": "skipped"
                                if now - slot > MAX_LATENESS
                                else "pending"
                                if snapshot is not None
                                else "unavailable",
                                "last_error": "missed_schedule"
                                if now - slot > MAX_LATENESS
                                else ""
                                if snapshot is not None
                                else "no_saved_forecast",
                            },
                        )
                        ids.append(delivery.pk)
    return ids
