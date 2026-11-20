from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobStatus,
    CollectionSchedule,
    RequestAttempt,
)
from pyracast.forecasts.timing import approval_job, effective_forecast_schedule
from pyracast.local_time import local_slot
from pyracast.regions.models import active_region


def remaining_forecast_requests(account: ProviderAccount, now: datetime) -> int:
    day_start = datetime.combine(now.astimezone(UTC).date(), datetime.min.time(), UTC)
    day_end = day_start + timedelta(days=1)
    attempted = set(
        RequestAttempt.objects.filter(provider_account=account, utc_day=day_start.date())
        .exclude(job_id=None)
        .values_list("job_id", flat=True)
    )
    needed: set[tuple[int, datetime]] = set()
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
        _, first_slot = approval_job(binding.pk) or (None, None)
        timing = effective_forecast_schedule(
            first_slot=first_slot,
            schedule=CollectionSchedule.objects.filter(region_id=binding.region_id).first(),
            daily_quota=account.daily_quota,
        )
        if timing is None:
            continue
        jobs = list(
            CollectionJob.objects.filter(binding=binding, endpoint=CollectionEndpoint.FORECASTS)
        )
        paid_or_terminal = {
            job.scheduled_for
            for job in jobs
            if job.pk in attempted
            or job.status
            in {
                CollectionJobStatus.SUCCEEDED,
                CollectionJobStatus.FAILED,
                CollectionJobStatus.STALE,
            }
        }
        for job in jobs:
            if (
                job.status
                in {
                    CollectionJobStatus.PENDING,
                    CollectionJobStatus.RETRY,
                    CollectionJobStatus.RUNNING,
                }
                and job.pk not in attempted
                and job.available_at < day_end
            ):
                needed.add((binding.pk, job.scheduled_for))
        zone = ZoneInfo(binding.region.timezone)
        first = day_start.astimezone(zone).date()
        last = (day_end - timedelta(microseconds=1)).astimezone(zone).date()
        day = first
        while day <= last:
            for clock in timing.times:
                slot = local_slot(day, clock, zone)
                if (
                    slot is not None
                    and max(now, timing.effective_from, day_start) <= slot < day_end
                    and slot not in paid_or_terminal
                ):
                    needed.add((binding.pk, slot))
            day += timedelta(days=1)
    return len(needed)
