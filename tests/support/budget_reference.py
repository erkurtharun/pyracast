from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import CollectionJob, CollectionSchedule, RequestAttempt
from pyracast.local_time import local_slot
from pyracast.regions.models import active_region

REOPENS_NOT = {"quota_exhausted", "schedule_disabled"}


def reference_forecast_requests(account: ProviderAccount, now: datetime) -> int:
    """The forecast budget as computed before collection timing had one function, with the due
    slot the scheduler opens again, the starts per UTC day claim admits, no job older than the
    due slot that claim ends unsent, and a job refused for the key counted only where the
    current schedule collects it."""
    day_start = datetime.combine(now.astimezone(UTC).date(), datetime.min.time(), UTC)
    day_end = day_start + timedelta(days=1)
    attempted = set(
        RequestAttempt.objects.filter(provider_account=account, utc_day=day_start.date())
        .exclude(job_id=None)
        .values_list("job_id", flat=True)
    )
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
        paid_or_terminal = {
            job.scheduled_for
            for job in jobs
            if job.pk in attempted or job.status in {"succeeded", "failed", "stale"}
        }
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
            superseded = due is not None and job.scheduled_for < due
            if (
                (job.status == "running" or (job.status in {"pending", "retry"} and not superseded))
                and job.pk not in attempted
                and job.available_at < day_end
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
                    and slot not in paid_or_terminal
                ):
                    needed.setdefault(slot, None)
            day += timedelta(days=1)
        if due is not None:
            due_job = by_slot.get(due)
            if due_job is None or (
                due_job.status == "stale"
                and due_job.last_error not in REOPENS_NOT
                and due_job.pk not in attempted
            ):
                needed.setdefault(due, due_job)
        latest = max(
            (job for job in jobs if job.scheduled_for <= now),
            key=lambda job: (job.scheduled_for, job.pk),
            default=None,
        )
        if (
            latest is not None
            and (latest.status, latest.last_error) == ("failed", "provider_authentication_failed")
            and latest.scheduled_for >= effective
            and (
                latest.pk == first_id
                or latest.scheduled_for.astimezone(zone).strftime("%H:%M") in clocks
            )
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
