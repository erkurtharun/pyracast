from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import QuerySet

from pyracast.collection_window import SLOT_SEARCH_DAYS
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobError,
    CollectionJobStatus,
    CollectionSchedule,
)
from pyracast.local_time import local_slot

HISTORY_REQUESTS_PER_DAY = 1


@dataclass(frozen=True)
class ForecastTiming:
    times: tuple[str, ...]
    effective_from: datetime
    first_job_id: int


def default_times() -> list[str]:
    return [settings.PYRACAST_CUTOFF_LOCAL.strftime("%H:%M")]


def forecast_time_limit(daily_quota: int) -> int:
    return max(daily_quota - HISTORY_REQUESTS_PER_DAY, 0)


def approval_job(binding_id: int) -> tuple[int, datetime] | None:
    # Approval creates a binding's first forecasts job (plants.approval.approve_region)
    # and the binding's schedule starts at its slot. Restored collections are created later
    # with earlier slots, so creation order, not the slot, finds it.
    return (
        CollectionJob.objects.filter(binding_id=binding_id, endpoint=CollectionEndpoint.FORECASTS)
        .order_by("created_at", "pk")
        .values_list("pk", "scheduled_for")
        .first()
    )


def latest_forecast_job(binding_id: int, moment: datetime) -> QuerySet[CollectionJob]:
    return CollectionJob.objects.filter(
        binding_id=binding_id,
        endpoint=CollectionEndpoint.FORECASTS,
        reference_import__isnull=True,
        scheduled_for__lte=moment,
    ).order_by("-scheduled_for", "-pk")


def on_the_schedule(job: CollectionJob, timing: ForecastTiming, timezone: str) -> bool:
    """Claim collects a slot-bound job only at one of the schedule's clocks or as the approval
    job (jobs.claim._stale_reason)."""
    clock = job.scheduled_for.astimezone(ZoneInfo(timezone)).strftime("%H:%M")
    return clock in timing.times or (
        job.pk == timing.first_job_id and job.scheduled_for >= timing.effective_from
    )


def awaits_validation(binding: SiteBinding, account: ProviderAccount) -> bool:
    """A validation of the binding can still reopen its job refused for the key: the binding is
    not validated with the account's key, or that key is recorded as rejected."""
    return (
        binding.validated_credential_revision != account.credential_revision
        or account.authentication_failed_revision == account.credential_revision
    )


def armed_again_by_validation(
    job: CollectionJob,
    timing: ForecastTiming,
    timezone: str,
    due: datetime | None,
    *,
    awaited: bool,
) -> bool:
    """A successful validation its binding `awaited` reopens the binding's latest forecast job
    refused for the key while the schedule in force still collects it. One from before the
    schedule took effect (a restore, an edit) stays ended, and so does one older than the due
    slot, which the scheduler opens instead."""
    return (
        awaited
        and job.status == CollectionJobStatus.FAILED
        and job.last_error == CollectionJobError.PROVIDER_AUTHENTICATION_FAILED
        and job.scheduled_for >= timing.effective_from
        and on_the_schedule(job, timing, timezone)
        and (due is None or job.scheduled_for >= due)
    )


def forecast_timing(
    binding: SiteBinding, schedule: CollectionSchedule | None, *, daily_quota: int | None = None
) -> ForecastTiming | None:
    first = approval_job(binding.pk)
    if first is None or (schedule is not None and not schedule.enabled):
        return None
    first_job_id, first_slot = first
    times = schedule.times if schedule is not None else default_times()
    effective_from = (
        max(first_slot, schedule.effective_from) if schedule is not None else first_slot
    )
    limit = forecast_time_limit(
        binding.provider_account.daily_quota if daily_quota is None else daily_quota
    )
    return ForecastTiming(tuple(times[:limit]), effective_from, first_job_id)


def latest_forecast_slot(
    now: datetime, timezone: str, times: Sequence[str], effective_from: datetime
) -> datetime | None:
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    return max(
        (
            slot
            for day in (today - timedelta(days=1), today)
            for clock in times
            if (slot := local_slot(day, clock, zone)) is not None and effective_from <= slot <= now
        ),
        default=None,
    )


def next_forecast_slot(
    now: datetime, timezone: str, times: Sequence[str], effective_from: datetime
) -> datetime | None:
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    for offset in range(SLOT_SEARCH_DAYS):
        slots = [
            slot
            for clock in times
            if (slot := local_slot(today + timedelta(days=offset), clock, zone)) is not None
            and now < slot
            and effective_from <= slot
        ]
        if slots:
            return min(slots)
    return None
