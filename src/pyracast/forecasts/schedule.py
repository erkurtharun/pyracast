from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Min
from django.utils.translation import gettext

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import WorkspaceRole
from pyracast.collection_window import (
    HISTORY_CLOCK,
    HISTORY_END_CLOCK,
    NIGHT_RETRY_DELAY,
    next_nightly_start,
)
from pyracast.connections.currency import binding_is_current
from pyracast.connections.models import MAX_DAILY_QUOTA
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobError,
    CollectionJobStatus,
    CollectionSchedule,
    ForecastSnapshot,
)
from pyracast.forecasts.timing import (
    HISTORY_REQUESTS_PER_DAY,
    default_times,
    forecast_time_limit,
    forecast_timing,
    next_forecast_slot,
)
from pyracast.local_time import CLOCK_PATTERN
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, Region


def get_schedule(tenant_id: int, region_id: int, now: datetime) -> CollectionSchedule:
    region = (
        Region.objects.active()
        .select_related("approved_binding__provider_account")
        .get(pk=region_id, tenant_id=tenant_id)
    )
    return CollectionSchedule.objects.filter(region=region).first() or CollectionSchedule(
        region=region,
        enabled=True,
        times=default_times(),
        effective_from=now,
    )


def configure_schedule(
    actor: User, tenant_id: int, region_id: int, *, enabled: bool, times: list[str], now: datetime
) -> CollectionSchedule:
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        region = (
            Region.objects.active()
            .select_for_update(of=("self",))
            .select_related("approved_binding__provider_account")
            .get(pk=region_id, tenant_id=tenant_id)
        )
        binding = region.approved_binding
        limit = forecast_time_limit(
            binding.provider_account.daily_quota if binding else MAX_DAILY_QUOTA
        )
        if len(times) != len(set(times)) or (enabled and len(times) > limit):
            raise ValidationError(
                gettext(
                    "Provide at most %(limit)s distinct local times; "
                    "history uses one daily request."
                ),
                params={"limit": limit},
            )
        if any(CLOCK_PATTERN.fullmatch(clock) is None for clock in times):
            raise ValidationError(gettext("Collection times must use HH:MM."))
        # The intent is kept in every approval state; readers gate collection on the binding.
        if enabled and not times:
            raise ValidationError(gettext("At least one time is required."))
        times = sorted(times)
        existing = CollectionSchedule.objects.filter(region=region).first()
        if existing is not None and existing.enabled == enabled and existing.times == times:
            return existing
        schedule, _ = CollectionSchedule.objects.update_or_create(
            region=region,
            defaults={"enabled": enabled, "times": times, "effective_from": now},
        )
        unclaimed = CollectionJob.objects.filter(
            binding__region=region,
            status__in=(CollectionJobStatus.PENDING, CollectionJobStatus.RETRY),
        )
        if enabled:
            zone = ZoneInfo(region.timezone)
            retained = [
                pk
                for pk, endpoint, slot, target in unclaimed.values_list(
                    "pk", "endpoint", "scheduled_for", "target_date"
                )
                if (
                    endpoint != CollectionEndpoint.FORECASTS
                    and target != slot.astimezone(zone).date()
                )
                or slot.astimezone(zone).strftime("%H:%M") in times
            ]
            unclaimed = unclaimed.exclude(pk__in=retained)
        unclaimed.update(
            status=CollectionJobStatus.STALE,
            last_error=CollectionJobError.SCHEDULE_CHANGED,
            lease_token=None,
            lease_expires_at=None,
        )
        record_audit(
            actor=actor,
            region=region,
            action=AuditAction.COLLECTION_SCHEDULE_UPDATED,
            revision=region.revision,
        )
        return schedule


def _next_collection_at(schedule: CollectionSchedule, now: datetime) -> datetime | None:
    binding = schedule.region.approved_binding
    if (
        binding is None
        or binding.validated_at is None
        or not binding_is_current(binding, schedule.region)
    ):
        return None
    timing = forecast_timing(binding, schedule)
    if timing is None:
        return None
    queued = CollectionJob.objects.filter(
        binding=binding, status__in=(CollectionJobStatus.PENDING, CollectionJobStatus.RETRY)
    ).aggregate(next=Min("available_at"))
    zone = schedule.region.timezone
    planned = next_forecast_slot(now, zone, timing.times, timing.effective_from)
    moments = (queued["next"], planned, next_nightly_start(now, zone))
    return min((moment for moment in moments if moment is not None), default=None)


def schedule_payload(schedule: CollectionSchedule, now: datetime) -> dict[str, object]:
    binding = schedule.region.approved_binding
    limit = binding.provider_account.daily_quota if binding else MAX_DAILY_QUOTA
    jobs = CollectionJob.objects.filter(binding=binding)
    return {
        "enabled": schedule.enabled,
        "times": schedule.times,
        "timezone": schedule.region.timezone,
        "effective_from": schedule.effective_from,
        "last_success_at": ForecastSnapshot.objects.filter(
            binding=binding,
            job__endpoint__in=(CollectionEndpoint.FORECASTS, CollectionEndpoint.ESTIMATED_ACTUALS),
            job__reference_import__isnull=True,
            job__status=CollectionJobStatus.SUCCEEDED,
        ).aggregate(last=Max("collected_at"))["last"]
        if binding
        else None,
        "next_collection_at": _next_collection_at(schedule, now),
        "recent_jobs": list(
            jobs.order_by("-scheduled_for", "-pk").values(
                "id",
                "endpoint",
                "status",
                "scheduled_for",
                "available_at",
                "retry_until",
                "attempts",
                "last_error",
            )[:6]
        )
        if binding
        else [],
        "history_requests_reserved_per_day": HISTORY_REQUESTS_PER_DAY,
        "history_local_time": HISTORY_CLOCK,
        "history_retry_until_local": HISTORY_END_CLOCK,
        "history_retry_minutes": int(NIGHT_RETRY_DELAY.total_seconds() / 60),
        "max_forecast_times": forecast_time_limit(limit),
    }
