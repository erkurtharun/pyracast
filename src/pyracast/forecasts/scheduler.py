from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError

from pyracast.forecasts.models import CollectionJob
from pyracast.regions.models import Region


def enqueue_due(now: datetime, cutoff_hour: int | None = None) -> list[int]:
    if now.tzinfo is None or (cutoff_hour is not None and not 0 <= cutoff_hour <= 23):
        raise ValidationError("An aware timestamp and valid cutoff hour are required.")
    cutoff: time = settings.PYRACAST_CUTOFF_LOCAL if cutoff_hour is None else time(cutoff_hour)
    jobs: list[int] = []
    for region in Region.objects.filter(status="approved").select_related("approved_binding"):
        binding = region.approved_binding
        if binding is None:
            continue
        zone = ZoneInfo(region.timezone)
        local_now = now.astimezone(zone)
        slot_date = local_now.date()
        slot = datetime.combine(slot_date, cutoff, tzinfo=zone).astimezone(UTC)
        if slot > now:
            slot_date -= timedelta(days=1)
            slot = datetime.combine(slot_date, cutoff, tzinfo=zone).astimezone(UTC)
        if binding.validated_at is None or slot < binding.validated_at:
            continue
        job, _ = CollectionJob.objects.get_or_create(
            binding=binding,
            endpoint="forecasts",
            scheduled_slot=slot,
            defaults={"target_date": slot_date + timedelta(days=1), "available_at": now},
        )
        jobs.append(job.pk)
    return jobs
