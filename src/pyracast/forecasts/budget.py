from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Exists, F, OuterRef, Q, QuerySet

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobStatus,
    CollectionSchedule,
    RequestAttempt,
    RequestPurpose,
)
from pyracast.forecasts.scheduler import SLOT_FINAL_ERRORS
from pyracast.forecasts.timing import (
    ForecastTiming,
    armed_again_by_validation,
    forecast_timing,
    latest_forecast_job,
    latest_forecast_slot,
)
from pyracast.local_time import local_slot
from pyracast.regions.models import RegionStatus, active_region

OPEN = (CollectionJobStatus.PENDING, CollectionJobStatus.RETRY, CollectionJobStatus.RUNNING)
ENDED = (CollectionJobStatus.SUCCEEDED, CollectionJobStatus.FAILED, CollectionJobStatus.STALE)


def approved_bindings(account: ProviderAccount) -> QuerySet[SiteBinding]:
    """The account's plants that collect now or once they are revalidated with its key."""
    return SiteBinding.objects.filter(
        active_region("region__"),
        provider_account=account,
        region__status=RegionStatus.APPROVED,
        region__approved_binding=F("pk"),
        region__revision=F("region_revision"),
    )


def forecast_starts(binding_id: int, timing: ForecastTiming, utc_day: date) -> int:
    """Forecast jobs the binding started on the UTC day; claim admits `len(timing.times)`."""
    earlier = RequestAttempt.objects.filter(job_id=OuterRef("job_id"), utc_day__lt=utc_day)
    return (
        RequestAttempt.objects.filter(
            binding_id=binding_id,
            purpose=RequestPurpose.FORECAST,
            utc_day=utc_day,
            job__isnull=False,
        )
        .exclude(job_id=timing.first_job_id)
        .exclude(Exists(earlier))
        .values("job_id")
        .distinct()
        .count()
    )


def started_jobs(timing: ForecastTiming, job_ids: list[int]) -> set[int]:
    """Jobs claim admits without a start of the day: the approval job and any job sent before."""
    sent = set(RequestAttempt.objects.filter(job_id__in=job_ids).values_list("job_id", flat=True))
    return {pk for pk in job_ids if pk == timing.first_job_id or pk in sent}


def _jobs_still_sent(
    binding: SiteBinding, timing: ForecastTiming, now: datetime
) -> dict[datetime, CollectionJob | None]:
    day_start = datetime.combine(now.astimezone(UTC).date(), datetime.min.time(), UTC)
    day_end = day_start + timedelta(days=1)
    jobs = list(
        CollectionJob.objects.filter(
            Q(status__in=OPEN) | Q(scheduled_for__gte=day_start, scheduled_for__lt=day_end),
            binding=binding,
            endpoint=CollectionEndpoint.FORECASTS,
            reference_import__isnull=True,
        )
    )
    attempted = set(
        RequestAttempt.objects.filter(job__binding=binding, utc_day=day_start.date()).values_list(
            "job_id", flat=True
        )
    )
    paid_or_ended = {
        job.scheduled_for for job in jobs if job.pk in attempted or job.status in ENDED
    }
    sent: dict[datetime, CollectionJob | None] = {
        job.scheduled_for: job
        for job in jobs
        if job.status in OPEN and job.pk not in attempted and job.available_at < day_end
    }
    zone = ZoneInfo(binding.region.timezone)
    day = day_start.astimezone(zone).date()
    last = (day_end - timedelta(microseconds=1)).astimezone(zone).date()
    while day <= last:
        for clock in timing.times:
            slot = local_slot(day, clock, zone)
            if (
                slot is not None
                and max(now, timing.effective_from, day_start) <= slot < day_end
                and slot not in paid_or_ended
            ):
                sent.setdefault(slot, None)
        day += timedelta(days=1)
    # The slot the scheduler opens, or opens again, once the binding is current.
    due = latest_forecast_slot(now, binding.region.timezone, timing.times, timing.effective_from)
    if due is not None:
        job = CollectionJob.objects.filter(
            binding=binding, endpoint=CollectionEndpoint.FORECASTS, scheduled_for=due
        ).first()
        if job is None or (
            job.status == CollectionJobStatus.STALE
            and job.last_error not in SLOT_FINAL_ERRORS
            and job.pk not in attempted
        ):
            sent.setdefault(due, job)
    latest = latest_forecast_job(binding.pk, now).first()
    if latest is not None and armed_again_by_validation(latest):
        sent[latest.scheduled_for] = latest
    return sent


def forecast_need(binding: SiteBinding, now: datetime, daily_quota: int) -> int:
    """Forecast requests the binding still sends in the UTC day of `now`, as claim admits them."""
    timing = forecast_timing(
        binding,
        CollectionSchedule.objects.filter(region_id=binding.region_id).first(),
        daily_quota=daily_quota,
    )
    if timing is None:
        return 0
    sent = _jobs_still_sent(binding, timing, now)
    free = started_jobs(timing, [job.pk for job in sent.values() if job is not None])
    starts = sum(1 for job in sent.values() if job is None or job.pk not in free)
    left = max(
        len(timing.times) - forecast_starts(binding.pk, timing, now.astimezone(UTC).date()), 0
    )
    return len(sent) - starts + min(starts, left)


def remaining_forecast_requests(
    account: ProviderAccount, now: datetime, *, validating: SiteBinding | None = None
) -> int:
    """What every plant that collects now or once revalidated still needs today. Validating a
    plant that awaits revalidation counts only the plants that collect now and that plant."""
    plants = list(approved_bindings(account).select_related("region", "provider_account"))
    waiting = {
        binding.pk
        for binding in plants
        if binding.validated_credential_revision != account.credential_revision
    }
    if validating is not None and validating.pk in waiting:
        plants = [
            binding
            for binding in plants
            if binding.pk == validating.pk or binding.pk not in waiting
        ]
    return sum(forecast_need(binding, now, account.daily_quota) for binding in plants)
