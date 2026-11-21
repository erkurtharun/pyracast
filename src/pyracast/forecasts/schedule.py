from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, DateTimeField, F, Max, Min, Q, Value
from django.db.models.functions import Greatest
from django.utils.translation import gettext

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import WorkspaceRole
from pyracast.collection_window import (
    HISTORY_CLOCK,
    HISTORY_END_CLOCK,
    NIGHT_RETRY_DELAY,
    next_nightly_start,
)
from pyracast.connections.models import MAX_DAILY_QUOTA
from pyracast.forecasts.jobs.claim import claimable_from, open_at_claim
from pyracast.forecasts.jobs.ready import WAITING
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionInactiveReason,
    CollectionJob,
    CollectionJobError,
    CollectionJobStatus,
    CollectionSchedule,
    CollectionState,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.forecasts.quota import deferral
from pyracast.forecasts.scheduler import (
    SLOT_FINAL_ERRORS,
    Collecting,
    collection_gate,
    open_night_window,
)
from pyracast.forecasts.timing import (
    HISTORY_REQUESTS_PER_DAY,
    default_times,
    forecast_time_limit,
    latest_forecast_slot,
    next_forecast_slot,
)
from pyracast.local_time import CLOCK_PATTERN
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, Region, RegionStatus


def get_schedule(tenant_id: int, region_id: int, now: datetime) -> CollectionSchedule:
    region = (
        Region.objects.active()
        .select_related("approved_binding__provider_account", "collectionschedule")
        .get(pk=region_id, tenant_id=tenant_id)
    )
    try:
        return region.collectionschedule
    except CollectionSchedule.DoesNotExist:
        return CollectionSchedule(
            region=region, enabled=True, times=default_times(), effective_from=now
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
            status__in=WAITING,
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


@dataclass(frozen=True)
class CollectionStatus:
    state: CollectionState
    inactive_reason: CollectionInactiveReason | None
    next_collection_at: datetime | None


def _next_collection_at(collecting: Collecting, timezone: str, now: datetime) -> datetime | None:
    """The scheduler's next act: a missing nightly history job, one it revives, the latest slot's
    job it opens again, a queued job claim takes, a slot."""
    night = open_night_window(now, timezone)
    timing = collecting.timing
    slot = latest_forecast_slot(now, timezone, timing.times, timing.effective_from)
    moment = Value(now, output_field=DateTimeField())
    jobs = CollectionJob.objects.filter(binding=collecting.binding).aggregate(
        queued=Min(claimable_from(now), filter=Q(status__in=WAITING) & open_at_claim(now)),
        **(
            {
                "history": Count("pk", filter=night.history()),
                # The scheduler revives it at Greatest(available_at, now).
                "revived": Min(Greatest(F("available_at"), moment), filter=night.revivable()),
            }
            if night is not None
            else {}
        ),
        **(
            {
                "reopened": Count(
                    "pk",
                    filter=Q(
                        endpoint=CollectionEndpoint.FORECASTS,
                        scheduled_for=slot,
                        status=CollectionJobStatus.STALE,
                    )
                    & ~Q(last_error__in=SLOT_FINAL_ERRORS),
                )
            }
            if slot is not None
            else {}
        ),
    )
    due_now = (night is not None and not jobs["history"]) or (
        slot is not None and bool(jobs["reopened"])
    )
    moments = (
        now if due_now else None,
        jobs.get("revived"),
        jobs["queued"],
        next_forecast_slot(now, timezone, timing.times, timing.effective_from),
        next_nightly_start(now, timezone),
    )
    return min((moment for moment in moments if moment is not None), default=None)


def collection_status(schedule: CollectionSchedule, now: datetime) -> CollectionStatus:
    region = schedule.region
    gate = collection_gate(region, schedule if schedule.pk is not None else None)
    if not isinstance(gate, Collecting):
        pending = region.status == RegionStatus.PENDING
        state = CollectionState.WAITING_FOR_APPROVAL if pending else CollectionState.PAUSED
        return CollectionStatus(state, gate, None)
    next_at = _next_collection_at(gate, region.timezone, now)
    if deferral(gate.binding.provider_account, now) is not None:
        return CollectionStatus(
            CollectionState.PROVIDER_ATTENTION,
            CollectionInactiveReason.PROVIDER_UNAVAILABLE,
            next_at,
        )
    reason = None if gate.timing.times else CollectionInactiveReason.QUOTA_TOO_LOW
    return CollectionStatus(CollectionState.COLLECTING, reason, next_at)


def schedule_payload(schedule: CollectionSchedule, now: datetime) -> dict[str, object]:
    binding = schedule.region.approved_binding
    limit = binding.provider_account.daily_quota if binding else MAX_DAILY_QUOTA
    jobs = CollectionJob.objects.filter(binding=binding)
    status = collection_status(schedule, now)
    return {
        "enabled": schedule.enabled,
        "times": schedule.times,
        "timezone": schedule.region.timezone,
        "effective_from": schedule.effective_from,
        "state": status.state,
        "inactive_reason": status.inactive_reason,
        "requests_today": RequestAttempt.objects.filter(
            binding=binding, utc_day=now.astimezone(UTC).date()
        ).count()
        if binding
        else 0,
        "last_success_at": ForecastSnapshot.objects.filter(
            binding=binding,
            job__endpoint__in=(CollectionEndpoint.FORECASTS, CollectionEndpoint.ESTIMATED_ACTUALS),
            job__reference_import__isnull=True,
            job__status=CollectionJobStatus.SUCCEEDED,
        ).aggregate(last=Max("collected_at"))["last"]
        if binding
        else None,
        "next_collection_at": status.next_collection_at,
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
