from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Q

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobStatus,
    CollectionSchedule,
    RequestAttempt,
)
from pyracast.forecasts.timing import forecast_timing
from pyracast.local_time import local_slot
from pyracast.regions.models import active_region

OPEN = (CollectionJobStatus.PENDING, CollectionJobStatus.RETRY, CollectionJobStatus.RUNNING)
ENDED = (CollectionJobStatus.SUCCEEDED, CollectionJobStatus.FAILED, CollectionJobStatus.STALE)


def remaining_forecast_requests(account: ProviderAccount, now: datetime) -> int:
    day_start = datetime.combine(now.astimezone(UTC).date(), datetime.min.time(), UTC)
    day_end = day_start + timedelta(days=1)
    attempted = set(
        RequestAttempt.objects.filter(provider_account=account, utc_day=day_start.date())
        .exclude(job_id=None)
        .values_list("job_id", flat=True)
    )
    needed: set[tuple[int, datetime]] = set()
    # A plant still validated with an earlier key collects again once it is revalidated.
    bindings = SiteBinding.objects.filter(
        active_region("region__"),
        provider_account=account,
        region__approved_binding_id__isnull=False,
        region__status="approved",
    ).select_related("region", "provider_account")
    for binding in bindings:
        if binding.region.approved_binding_id != binding.pk:
            continue
        timing = forecast_timing(
            binding, CollectionSchedule.objects.filter(region_id=binding.region_id).first()
        )
        if timing is None:
            continue
        jobs = list(
            CollectionJob.objects.filter(
                Q(status__in=OPEN) | Q(scheduled_for__gte=day_start, scheduled_for__lt=day_end),
                binding=binding,
                endpoint=CollectionEndpoint.FORECASTS,
                reference_import__isnull=True,
            )
        )
        paid_or_ended = {
            job.scheduled_for for job in jobs if job.pk in attempted or job.status in ENDED
        }
        for job in jobs:
            if job.status in OPEN and job.pk not in attempted and job.available_at < day_end:
                needed.add((binding.pk, job.scheduled_for))
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
                    needed.add((binding.pk, slot))
            day += timedelta(days=1)
    return len(needed)
