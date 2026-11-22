from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings

from pyracast.background.deadlines import ACTORS
from pyracast.background.retry_policy import PROVIDER_JOB
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.local_time import local_slot
from pyracast.regions.models import active_region

REOPENS_NOT = {"quota_exhausted", "schedule_disabled"}
WORKER_LEASE = ACTORS["collect_forecast"].lease


def at_a_kept_clock(
    job: CollectionJob, zone: ZoneInfo, clocks: list[str], first_id: int, effective: datetime
) -> bool:
    return (job.pk == first_id and job.scheduled_for >= effective) or (
        job.scheduled_for.astimezone(zone).strftime("%H:%M") in clocks
    )


def reference_forecast_requests(account: ProviderAccount, now: datetime) -> int:
    """The forecast budget as computed before collection timing had one function. Each slot of
    the UTC day still to come, and the due slot the scheduler opens again, costs a request unless
    a job already sits at it. A job whose provider answer is stored costs nothing more. A job a
    worker runs under its lease costs one unless the request it made since it was taken is out
    and not refused. Any other open job costs one when claim would take it before the day ends:
    not older than the due slot, at a clock of the saved schedule (or the approval job), with
    claims left, and ready by its time or at once if it is throttled and its account's delay was
    cleared. A job refused for the key costs one where the current schedule collects it while a
    validation can still reopen it: these plants are validated with the current key, so only
    while that key is recorded as rejected. Starts per UTC day are bounded as claim admits
    them."""
    day_start = datetime.combine(now.astimezone(UTC).date(), datetime.min.time(), UTC)
    day_end = day_start + timedelta(days=1)
    total = 0
    bindings = SiteBinding.objects.filter(
        active_region("region__"),
        provider_account=account,
        region__approved_binding_id__isnull=False,
        region__status="approved",
        validated_credential_revision=account.credential_revision,
    ).select_related("region")
    for binding in bindings:
        if binding.region.approved_binding_id != binding.pk:
            continue
        schedule = CollectionSchedule.objects.filter(region_id=binding.region_id).first()
        first = (
            CollectionJob.objects.filter(binding=binding, endpoint="forecasts")
            .order_by("created_at", "pk")
            .values_list("pk", "scheduled_for")
            .first()
        )
        if first is None or (schedule is not None and not schedule.enabled):
            continue
        first_id, first_slot = first
        clocks = schedule.times if schedule else [settings.PYRACAST_CUTOFF_LOCAL.strftime("%H:%M")]
        clocks = clocks[: max(account.daily_quota - 1, 0)]
        effective = max(first_slot, schedule.effective_from) if schedule else first_slot
        jobs = list(CollectionJob.objects.filter(binding=binding, endpoint="forecasts"))
        by_slot = {job.scheduled_for: job for job in jobs}
        answered = set(
            ForecastSnapshot.objects.filter(job__in=jobs).values_list("job_id", flat=True)
        )
        zone = ZoneInfo(binding.region.timezone)
        local_today = now.astimezone(zone).date()
        passed = [
            slot
            for day in (local_today - timedelta(days=1), local_today)
            for clock in clocks
            if (slot := local_slot(day, clock, zone)) is not None and effective <= slot <= now
        ]
        due = max(passed, default=None)
        needed: dict[datetime, CollectionJob | None] = {}
        for job in jobs:
            if job.status not in {"pending", "retry", "running"} or job.pk in answered:
                continue
            if (
                job.status == "running"
                and job.lease_expires_at is not None
                and job.lease_expires_at > now
            ):
                out = RequestAttempt.objects.filter(
                    job=job,
                    created_at__gte=job.lease_expires_at - WORKER_LEASE,
                    outcome__in=("uncertain", "succeeded"),
                ).exists()
                if not out:
                    needed[job.scheduled_for] = job
                continue
            superseded = due is not None and job.scheduled_for < due
            spent = job.retry_until is None and job.attempts >= PROVIDER_JOB.max_attempts
            released = (job.status, job.last_error) == ("retry", "provider_throttled") and (
                account.request_blocked_until is None
            )
            if (
                not superseded
                and not spent
                and (schedule is None or at_a_kept_clock(job, zone, clocks, first_id, effective))
                and (released or job.available_at < day_end)
            ):
                needed[job.scheduled_for] = job
        day = day_start.astimezone(zone).date()
        last = (day_end - timedelta(microseconds=1)).astimezone(zone).date()
        while day <= last:
            for clock in clocks:
                slot = local_slot(day, clock, zone)
                if (
                    slot is not None
                    and max(now, effective, day_start) <= slot < day_end
                    and slot not in by_slot
                ):
                    needed.setdefault(slot, None)
            day += timedelta(days=1)
        if due is not None:
            due_job = by_slot.get(due)
            if due_job is None or (
                due_job.status == "stale"
                and due_job.last_error not in REOPENS_NOT
                and due_job.pk not in answered
            ):
                needed.setdefault(due, due_job)
        latest = max(
            (job for job in jobs if job.scheduled_for <= now),
            key=lambda job: (job.scheduled_for, job.pk),
            default=None,
        )
        if (
            latest is not None
            and account.authentication_failed_revision == account.credential_revision
            and (latest.status, latest.last_error) == ("failed", "provider_authentication_failed")
            and latest.scheduled_for >= effective
            and at_a_kept_clock(latest, zone, clocks, first_id, effective)
            and (due is None or latest.scheduled_for >= due)
        ):
            needed[latest.scheduled_for] = latest
        ever_sent = set(
            RequestAttempt.objects.filter(job__binding=binding).values_list("job_id", flat=True)
        )
        starts = sum(
            1
            for job in needed.values()
            if job is None or (job.pk != first_id and job.pk not in ever_sent)
        )
        started = {
            attempt.job_id
            for attempt in RequestAttempt.objects.filter(
                binding=binding, purpose="forecast", utc_day=day_start.date(), job__isnull=False
            ).exclude(job_id=first_id)
            if not RequestAttempt.objects.filter(
                job_id=attempt.job_id, utc_day__lt=day_start.date()
            ).exists()
        }
        total += len(needed) - starts + min(starts, max(len(clocks) - len(started), 0))
    return total
