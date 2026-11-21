import uuid
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from django.db import transaction

from pyracast.background.clock import Clock
from pyracast.background.deadlines import ACTORS
from pyracast.background.lease import carries, held, holding, release, take
from pyracast.background.retry_policy import PROVIDER_JOB
from pyracast.collection_window import INTRADAY_HISTORY_WINDOW, NIGHT_RETRY_DELAY
from pyracast.connections.currency import binding_is_current
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobError,
    CollectionJobStatus,
    CollectionSchedule,
)
from pyracast.forecasts.timing import forecast_timing, latest_forecast_slot
from pyracast.provider_retry import next_provider_day, next_retry_at
from pyracast.regions.models import Region

LEASE_DURATION = ACTORS["collect_forecast"].lease


@dataclass(frozen=True)
class Lease:
    job_id: int
    binding: SiteBinding
    target_date: date
    token: uuid.UUID
    endpoint: str = "forecasts"


class RetryKind(StrEnum):
    # Counts the attempt and backs off; the job fails once its attempts or its window run out.
    TRANSIENT = "transient"
    # Transient provider failure: as TRANSIENT with the forecast minimum backoff.
    PROVIDER = "provider"
    # Returns the attempt and backs off: nothing was asked of the provider.
    WAITING = "waiting"
    # Returns the attempt and waits for the provider day; past its window the job is skipped.
    PROVIDER_DAY = "provider_day"
    # Returns the attempt and fails: an operator must act first or the job's window has closed.
    BLOCKED = "blocked"
    # Returns the attempt and ends the job as stale: its binding may not collect it at all.
    SKIPPED = "skipped"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class Failure:
    code: CollectionJobError
    kind: RetryKind = RetryKind.TRANSIENT
    retry_after_seconds: int | None = None


def _is_intraday(job: CollectionJob) -> bool:
    return job.retry_until is not None and (
        job.retry_until <= job.scheduled_for + INTRADAY_HISTORY_WINDOW
    )


def _end_unclaimed(
    job: CollectionJob, status: CollectionJobStatus, code: CollectionJobError
) -> None:
    job.status = status
    job.last_error = code
    release(job)
    job.save(update_fields=("status", "last_error", "lease_token", "lease_expires_at"))


def _stale_reason(
    job: CollectionJob, binding: SiteBinding, now: datetime
) -> CollectionJobError | None:
    schedule = CollectionSchedule.objects.filter(region_id=binding.region_id).first()
    timing = forecast_timing(binding, schedule)
    latest_slot = (
        latest_forecast_slot(now, binding.timezone, timing.times, timing.effective_from)
        if timing is not None
        else None
    )
    if (
        job.endpoint == CollectionEndpoint.FORECASTS
        and latest_slot is not None
        and latest_slot > job.scheduled_for
    ):
        return CollectionJobError.FORECAST_SUPERSEDED
    if not binding_is_current(binding):
        return CollectionJobError.BINDING_NOT_CURRENT
    if schedule is None:
        return None
    if not schedule.enabled:
        return CollectionJobError.SCHEDULE_DISABLED
    slot_bound = job.endpoint == CollectionEndpoint.FORECASTS or (
        job.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
        and job.target_date == job.scheduled_for.astimezone(ZoneInfo(binding.timezone)).date()
    )
    clock = job.scheduled_for.astimezone(ZoneInfo(binding.region.timezone)).strftime("%H:%M")
    # Approval creates one immediate acquisition outside existing clocks.
    approval_acquisition = (
        job.endpoint == CollectionEndpoint.FORECASTS
        and job.scheduled_for >= schedule.effective_from
        and timing is not None
        and job.pk == timing.first_job_id
    )
    if (
        slot_bound
        and clock not in (timing.times if timing is not None else ())
        and not approval_acquisition
    ):
        return CollectionJobError.SLOT_NOT_CONFIGURED
    return None


def claim(job_id: int, now: datetime) -> Lease | None:
    clock = Clock(now)
    with transaction.atomic():
        region_id = (
            CollectionJob.objects.filter(pk=job_id)
            .values_list("binding__region_id", flat=True)
            .first()
        )
        if region_id is not None:
            Region.objects.select_for_update().get(pk=region_id)
        job = CollectionJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            return None
        now = clock.now()
        cleared_cooldown = (
            job.status == CollectionJobStatus.RETRY
            and job.last_error == CollectionJobError.PROVIDER_THROTTLED
            and CollectionJob.objects.filter(
                pk=job.pk, binding__provider_account__request_blocked_until__isnull=True
            ).exists()
        )
        if job.status in {
            CollectionJobStatus.SUCCEEDED,
            CollectionJobStatus.STALE,
            CollectionJobStatus.FAILED,
        } or (job.available_at > now and not cleared_cooldown):
            return None
        if job.retry_until is not None and now >= job.retry_until:
            _end_unclaimed(
                job, CollectionJobStatus.FAILED, CollectionJobError.HISTORY_WINDOW_CLOSED
            )
            return None
        if held(job, CollectionJobStatus.RUNNING, now):
            return None
        if (
            job.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
            and CollectionJob.objects.filter(
                holding(CollectionJobStatus.RUNNING, now),
                binding_id=job.binding_id,
                endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
            )
            .exclude(pk=job.pk)
            .exists()
        ):
            return None
        binding = SiteBinding.objects.select_related("provider_account", "region").get(
            pk=job.binding_id
        )
        reason = _stale_reason(job, binding, now)
        if reason == CollectionJobError.FORECAST_SUPERSEDED:
            _end_unclaimed(job, CollectionJobStatus.STALE, reason)
            return None
        if job.retry_until is None and PROVIDER_JOB.exhausted(job.attempts):
            _end_unclaimed(job, CollectionJobStatus.FAILED, CollectionJobError.ATTEMPTS_EXHAUSTED)
            return None
        if reason is not None:
            _end_unclaimed(job, CollectionJobStatus.STALE, reason)
            return None
        token = take(job, CollectionJobStatus.RUNNING, now, LEASE_DURATION)
        if cleared_cooldown:
            job.available_at = now
        if job.retry_until is not None:
            job.available_at = now + (LEASE_DURATION if _is_intraday(job) else NIGHT_RETRY_DELAY)
        job.save(
            update_fields=("status", "lease_token", "lease_expires_at", "attempts", "available_at")
        )
        return Lease(job.pk, binding, job.target_date, token, job.endpoint)


def _retry_at(job: CollectionJob, now: datetime, failure: Failure) -> datetime | None:
    """The next attempt, or None when the job ends."""
    waits_for_provider_day = failure.kind == RetryKind.PROVIDER_DAY
    if job.retry_until is not None:
        available = next_retry_at(
            now,
            job.attempts,
            failure.retry_after_seconds,
            minimum_seconds=120 * 2 ** min(job.attempts, 6)
            if _is_intraday(job) and not waits_for_provider_day
            else int(NIGHT_RETRY_DELAY.total_seconds()),
        )
        if waits_for_provider_day and failure.code != CollectionJobError.HISTORY_BUDGET_RESERVED:
            available = max(available, next_provider_day(now))
        return available if available < job.retry_until else None
    if waits_for_provider_day:
        return next_provider_day(now)
    if PROVIDER_JOB.exhausted(job.attempts):
        return None
    retry_after = failure.retry_after_seconds
    if failure.kind == RetryKind.PROVIDER and job.endpoint == CollectionEndpoint.FORECASTS:
        retry_after = max(retry_after or 0, 120 * 2 ** min(job.attempts, 6))
    return next_retry_at(now, job.attempts, retry_after)


def retry(lease: Lease, now: datetime, failure: Failure) -> None:
    with transaction.atomic():
        job = CollectionJob.objects.select_for_update().get(pk=lease.job_id)
        if not carries(job, lease.token, CollectionJobStatus.RUNNING):
            return
        job.last_error = failure.code
        if failure.kind in {
            RetryKind.WAITING,
            RetryKind.PROVIDER_DAY,
            RetryKind.BLOCKED,
            RetryKind.SKIPPED,
        }:
            job.attempts -= 1
        available = (
            None
            if failure.kind in {RetryKind.BLOCKED, RetryKind.PERMANENT, RetryKind.SKIPPED}
            else _retry_at(job, now, failure)
        )
        if available is not None:
            job.status = CollectionJobStatus.RETRY
            job.available_at = available
        elif failure.kind in {RetryKind.PROVIDER_DAY, RetryKind.SKIPPED}:
            # A refusal that outlives the job's window is a planned skip, not a failure.
            job.status = CollectionJobStatus.STALE
        else:
            job.status = CollectionJobStatus.FAILED
        release(job)
        job.save()
