import re
from datetime import UTC, datetime

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction

from pyracast.accounts.operations import require_member
from pyracast.forecasts.jobs import binding_is_current
from pyracast.forecasts.models import CollectionJob, CollectionSchedule, RequestAttempt
from pyracast.regions.models import AuditEvent, Region

MAX_COLLECTION_TIMES = 1440


def forecast_time_limit(daily_quota: int) -> int:
    return min(MAX_COLLECTION_TIMES, max(daily_quota - 1, 0))


def get_schedule(actor: User, tenant_id: int, region_id: int, now: datetime) -> CollectionSchedule:
    require_member(actor, tenant_id)
    region = Region.objects.select_related("approved_binding__provider_account").get(
        pk=region_id, tenant_id=tenant_id
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
    require_member(actor, tenant_id)
    with transaction.atomic():
        region = (
            Region.objects.select_for_update(of=("self",))
            .select_related("approved_binding__provider_account")
            .get(pk=region_id, tenant_id=tenant_id)
        )
        binding = region.approved_binding
        limit = forecast_time_limit(binding.provider_account.daily_quota if binding else 10)
        if len(times) != len(set(times)) or len(times) > limit:
            raise ValidationError(
                f"Provide at most {limit} distinct local times; history uses one daily request."
            )
        if any(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock) is None for clock in times):
            raise ValidationError("Collection times must use HH:MM.")
        if enabled and (not times or binding is None or not binding_is_current(binding)):
            raise ValidationError("An approved current region and at least one time are required.")
        schedule, _ = CollectionSchedule.objects.update_or_create(
            region=region,
            defaults={"enabled": enabled, "times": sorted(times), "effective_from": now},
        )
        CollectionJob.objects.filter(
            binding__region=region, status__in=("pending", "retry")
        ).update(status="stale", lease_token=None, lease_expires_at=None)
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
    used = (
        RequestAttempt.objects.filter(
            provider_account_id=binding.provider_account_id, utc_day=now.astimezone(UTC).date()
        ).count()
        if binding
        else 0
    )
    return {
        "enabled": schedule.enabled,
        "times": schedule.times,
        "timezone": schedule.region.timezone,
        "effective_from": schedule.effective_from,
        "daily_request_limit": limit,
        "used_today": used,
        "remaining_today": max(limit - used, 0),
        "history_requests_per_day": 1,
        "max_forecast_times": forecast_time_limit(limit),
    }
