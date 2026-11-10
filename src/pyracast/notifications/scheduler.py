from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction

from pyracast.accounts.models import Membership
from pyracast.forecasts.jobs import binding_is_current
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.notifications.models import NotificationDelivery, ReminderPreference
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
                    locked_region = Region.objects.select_for_update().filter(pk=region.pk).first()
                    if (
                        locked_region is None
                        or not binding_is_current(binding)
                        or not ReminderPreference.objects.filter(
                            pk=preference.pk, enabled=True, effective_from__lte=slot
                        ).exists()
                    ):
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
                    delivery, _ = NotificationDelivery.objects.get_or_create(
                        preference=preference,
                        scheduled_for=slot,
                        defaults={
                            "snapshot": snapshot,
                            "recipient": preference.email or preference.user.email,
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
