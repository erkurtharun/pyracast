import re
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Min
from django.utils.translation import gettext

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import Membership
from pyracast.collection_window import (
    HISTORY_CLOCK,
    HISTORY_END_CLOCK,
    NIGHT_RETRY_DELAY,
    next_nightly_start,
)
from pyracast.forecasts.jobs import binding_is_current
from pyracast.forecasts.models import CollectionJob, CollectionSchedule, ForecastSnapshot
from pyracast.forecasts.timing import effective_forecast_schedule, next_forecast_slot
from pyracast.regions.models import AuditEvent, Region

MAX_COLLECTION_TIMES = 1440


def forecast_time_limit(daily_quota: int) -> int:
    return min(MAX_COLLECTION_TIMES, max(daily_quota - 1, 0))


def get_schedule(actor: User, tenant_id: int, region_id: int, now: datetime) -> CollectionSchedule:
    tenant_membership(actor, tenant_id)
    region = Region.objects.select_related("approved_binding__provider_account").get(
        pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
    )
    return CollectionSchedule.objects.filter(region=region).first() or CollectionSchedule(
        region=region,
        enabled=True,
        times=[settings.PYRACAST_CUTOFF_LOCAL.strftime("%H:%M")],
        effective_from=now,
    )


def configure_schedule(
    actor: User, tenant_id: int, region_id: int, *, enabled: bool, times: list[str], now: datetime
) -> CollectionSchedule:
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=Membership.Role.ADMIN, lock=True)
        region = (
            Region.objects.select_for_update(of=("self",))
            .select_related("approved_binding__provider_account")
            .get(pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True)
        )
        binding = region.approved_binding
        limit = forecast_time_limit(binding.provider_account.daily_quota if binding else 10)
        if len(times) != len(set(times)) or len(times) > limit:
            raise ValidationError(
                gettext(
                    "Provide at most %(limit)s distinct local times; "
                    "history uses one daily request."
                ),
                params={"limit": limit},
            )
        if any(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock) is None for clock in times):
            raise ValidationError(gettext("Collection times must use HH:MM."))
        if enabled and (not times or binding is None or not binding_is_current(binding)):
            raise ValidationError(
                gettext("An approved current region and at least one time are required.")
            )
        times = sorted(times)
        existing = CollectionSchedule.objects.filter(region=region).first()
        if existing is not None and existing.enabled == enabled and existing.times == times:
            return existing
        schedule, _ = CollectionSchedule.objects.update_or_create(
            region=region,
            defaults={"enabled": enabled, "times": times, "effective_from": now},
        )
        unclaimed = CollectionJob.objects.filter(
            binding__region=region, status__in=("pending", "retry")
        )
        if enabled:
            zone = ZoneInfo(region.timezone)
            retained = [
                pk
                for pk, endpoint, slot, target in unclaimed.values_list(
                    "pk", "endpoint", "scheduled_slot", "target_date"
                )
                if (endpoint != "forecasts" and target != slot.astimezone(zone).date())
                or slot.astimezone(zone).strftime("%H:%M") in times
            ]
            unclaimed = unclaimed.exclude(pk__in=retained)
        unclaimed.update(status="stale", lease_token=None, lease_expires_at=None)
        AuditEvent.objects.create(
            actor=actor,
            region=region,
            action="collection_schedule_updated",
            revision=region.revision,
        )
        return schedule


def _next_collection_at(schedule: CollectionSchedule, now: datetime) -> datetime | None:
    binding = schedule.region.approved_binding
    if binding is None or binding.validated_at is None or not binding_is_current(binding):
        return None
    jobs = CollectionJob.objects.filter(binding=binding)
    timing = effective_forecast_schedule(
        first_slot=jobs.filter(endpoint="forecasts", reference_import__isnull=True)
        .order_by("scheduled_slot")
        .values_list("scheduled_slot", flat=True)
        .first(),
        schedule=schedule,
        daily_quota=binding.provider_account.daily_quota,
    )
    if timing is None:
        return None
    queued = jobs.filter(status__in=("pending", "retry")).aggregate(next=Min("available_at"))
    zone = schedule.region.timezone
    planned = next_forecast_slot(now, zone, timing.times, timing.effective_from)
    moments = (queued["next"], planned, next_nightly_start(now, zone))
    return min((moment for moment in moments if moment is not None), default=None)


def schedule_payload(schedule: CollectionSchedule, now: datetime) -> dict[str, object]:
    binding = schedule.region.approved_binding
    limit = binding.provider_account.daily_quota if binding else 10
    jobs = CollectionJob.objects.filter(binding=binding)
    return {
        "enabled": schedule.enabled,
        "times": schedule.times,
        "timezone": schedule.region.timezone,
        "effective_from": schedule.effective_from,
        "last_success_at": ForecastSnapshot.objects.filter(binding=binding).aggregate(
            last=Max("collected_at")
        )["last"]
        if binding
        else None,
        "next_collection_at": _next_collection_at(schedule, now),
        "recent_jobs": list(
            jobs.order_by("-scheduled_slot", "-pk").values(
                "id",
                "endpoint",
                "status",
                "scheduled_slot",
                "available_at",
                "retry_until",
                "attempts",
                "last_error",
            )[:6]
        )
        if binding
        else [],
        "history_requests_reserved_per_day": 1,
        "history_local_time": HISTORY_CLOCK,
        "history_retry_until_local": HISTORY_END_CLOCK,
        "history_retry_minutes": int(NIGHT_RETRY_DELAY.total_seconds() / 60),
        "max_forecast_times": forecast_time_limit(limit),
    }
