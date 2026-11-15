import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.utils.translation import gettext

from pyracast.accounts.operations import require_member, require_workspace_admin
from pyracast.collection_window import HISTORY_CLOCK, HISTORY_END_CLOCK, NIGHT_RETRY_DELAY
from pyracast.forecasts.jobs import binding_is_current
from pyracast.forecasts.models import CollectionJob, CollectionSchedule, RequestAttempt
from pyracast.regions.models import AuditEvent, Region

MAX_COLLECTION_TIMES = 1440


def forecast_time_limit(daily_quota: int) -> int:
    return min(MAX_COLLECTION_TIMES, max(daily_quota - 1, 0))


def get_schedule(actor: User, tenant_id: int, region_id: int, now: datetime) -> CollectionSchedule:
    require_member(actor, tenant_id)
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
        require_workspace_admin(actor, tenant_id)
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


def schedule_payload(schedule: CollectionSchedule, now: datetime) -> dict[str, object]:
    binding = schedule.region.approved_binding
    limit = binding.provider_account.daily_quota if binding else 10
    outcomes = {"succeeded": 0, "failed": 0, "uncertain": 0}
    utc_day = now.astimezone(UTC).date()
    if binding:
        counts = (
            RequestAttempt.objects.filter(
                provider_account_id=binding.provider_account_id, utc_day=now.astimezone(UTC).date()
            )
            .values("outcome")
            .annotate(count=Count("pk"))
        )
        outcomes.update({row["outcome"]: row["count"] for row in counts})
    used = sum(outcomes.values())
    recent = (
        list(
            CollectionJob.objects.filter(binding=binding)
            .order_by("-scheduled_slot", "-pk")
            .values(
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
        else []
    )
    return {
        "enabled": schedule.enabled,
        "times": schedule.times,
        "timezone": schedule.region.timezone,
        "effective_from": schedule.effective_from,
        "daily_request_limit": limit,
        "used_today": used,
        "remaining_today": max(limit - used, 0),
        "quota_utc_day": utc_day.isoformat(),
        "requests_by_outcome": outcomes,
        "recent_jobs": recent,
        "history_requests_reserved_per_day": 1,
        "history_local_time": HISTORY_CLOCK,
        "history_retry_until_local": HISTORY_END_CLOCK,
        "history_retry_minutes": int(NIGHT_RETRY_DELAY.total_seconds() / 60),
        "max_forecast_times": forecast_time_limit(limit),
    }
