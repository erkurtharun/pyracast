from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.functions import Greatest

from pyracast.collection_window import nightly_window
from pyracast.forecasts.jobs import binding_is_current
from pyracast.forecasts.models import CollectionJob, CollectionSchedule
from pyracast.forecasts.timing import latest_forecast_slot
from pyracast.regions.models import Region


def enqueue_due(now: datetime, cutoff_hour: int | None = None) -> list[int]:
    if now.tzinfo is None or (cutoff_hour is not None and not 0 <= cutoff_hour <= 23):
        raise ValidationError("An aware timestamp and valid cutoff hour are required.")
    cutoff: time = settings.PYRACAST_CUTOFF_LOCAL if cutoff_hour is None else time(cutoff_hour)
    jobs: list[int] = []
    for region_id in Region.objects.filter(status="approved", deleted_at__isnull=True).values_list(
        "pk", flat=True
    ):
        with transaction.atomic():
            region = (
                Region.objects.select_for_update(of=("self",))
                .select_related("approved_binding__provider_account")
                .filter(pk=region_id)
                .first()
            )
            if region is None:
                continue
            binding = region.approved_binding
            if binding is None or binding.validated_at is None or not binding_is_current(binding):
                continue
            first_slot = (
                CollectionJob.objects.filter(binding=binding, endpoint="forecasts")
                .order_by("scheduled_slot")
                .values_list("scheduled_slot", flat=True)
                .first()
            )
            if first_slot is None:
                continue
            schedule = CollectionSchedule.objects.filter(region=region).first()
            if schedule is not None and not schedule.enabled:
                continue
            times = schedule.times if schedule else [cutoff.strftime("%H:%M")]
            times = times[: max(binding.provider_account.daily_quota - 1, 0)]
            effective_from = max(first_slot, schedule.effective_from) if schedule else first_slot
            zone = ZoneInfo(region.timezone)
            today = now.astimezone(zone).date()
            window = nightly_window(today, region.timezone)
            if window is not None and window[0] <= now < window[1]:
                history, _ = CollectionJob.objects.get_or_create(
                    binding=binding,
                    endpoint="estimated_actuals",
                    scheduled_slot=window[0],
                    defaults={
                        "target_date": today - timedelta(days=1),
                        "available_at": now,
                        "retry_until": window[1],
                    },
                )
                CollectionJob.objects.filter(
                    binding=binding,
                    endpoint="estimated_actuals",
                    target_date=today - timedelta(days=1),
                    scheduled_slot__gte=window[0],
                    retry_until=window[1],
                    status="stale",
                    forecastsnapshot__isnull=True,
                ).update(
                    status="pending",
                    available_at=Greatest("available_at", now),
                    lease_token=None,
                    lease_expires_at=None,
                )
                jobs.append(history.pk)
            slot = latest_forecast_slot(now, region.timezone, times, effective_from)
            if slot is None:
                continue
            CollectionJob.objects.filter(
                binding=binding,
                endpoint="forecasts",
                scheduled_slot__lt=slot,
                status__in=("pending", "retry"),
            ).update(
                status="stale",
                last_error="forecast_superseded",
                lease_token=None,
                lease_expires_at=None,
            )
            job, _ = CollectionJob.objects.get_or_create(
                binding=binding,
                endpoint="forecasts",
                scheduled_slot=slot,
                defaults={
                    "target_date": slot.astimezone(zone).date() + timedelta(days=1),
                    "available_at": now,
                },
            )
            CollectionJob.objects.filter(pk=job.pk, status="stale").update(
                status="pending", available_at=now, lease_token=None, lease_expires_at=None
            )
            jobs.append(job.pk)
    return jobs
