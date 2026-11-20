from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import CollectionJob, CollectionSchedule, RequestAttempt
from pyracast.local_time import local_slot
from pyracast.regions.models import active_region


def reference_forecast_requests(account: ProviderAccount, now: datetime) -> int:
    """The forecast budget as computed before collection timing had one function."""
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
        schedule = CollectionSchedule.objects.filter(region_id=binding.region_id).first()
        first_slot = (
            CollectionJob.objects.filter(binding=binding, endpoint="forecasts")
            .order_by("created_at", "pk")
            .values_list("scheduled_for", flat=True)
            .first()
        )
        if first_slot is None or (schedule is not None and not schedule.enabled):
            continue
        clocks = schedule.times if schedule else [settings.PYRACAST_CUTOFF_LOCAL.strftime("%H:%M")]
        clocks = clocks[: max(account.daily_quota - 1, 0)]
        effective = max(first_slot, schedule.effective_from) if schedule else first_slot
        jobs = list(CollectionJob.objects.filter(binding=binding, endpoint="forecasts"))
        paid_or_terminal = {
            job.scheduled_for
            for job in jobs
            if job.pk in attempted or job.status in {"succeeded", "failed", "stale"}
        }
        for job in jobs:
            if (
                job.status in {"pending", "retry", "running"}
                and job.pk not in attempted
                and job.available_at < day_end
            ):
                needed.add((binding.pk, job.scheduled_for))
        zone = ZoneInfo(binding.region.timezone)
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
                    needed.add((binding.pk, slot))
            day += timedelta(days=1)
    return len(needed)
