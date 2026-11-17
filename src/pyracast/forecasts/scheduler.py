import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import close_old_connections, transaction
from django.db.models.functions import Greatest

from pyracast.background.failures import log_failure
from pyracast.collection_window import nightly_window
from pyracast.forecasts.jobs import binding_is_current
from pyracast.forecasts.models import CollectionJob, CollectionSchedule
from pyracast.forecasts.timing import effective_forecast_schedule, latest_forecast_slot
from pyracast.regions.models import Region

logger = logging.getLogger(__name__)


class CollectionSchedulingError(RuntimeError):
    def __init__(self, failed_region_ids: list[int]) -> None:
        super().__init__("Collection scheduling failed for one or more regions.")
        self.failed_region_ids = failed_region_ids


def _enqueue_region(region_id: int, now: datetime, cutoff: time) -> list[int]:
    jobs: list[int] = []
    with transaction.atomic():
        region = (
            Region.objects.select_for_update(of=("self",))
            .select_related("approved_binding__provider_account")
            .filter(pk=region_id)
            .first()
        )
        if region is None:
            return jobs
        binding = region.approved_binding
        if binding is None or binding.validated_at is None or not binding_is_current(binding):
            return jobs
        first_slot = (
            CollectionJob.objects.filter(
                binding=binding, endpoint="forecasts", reference_import__isnull=True
            )
            .order_by("scheduled_slot")
            .values_list("scheduled_slot", flat=True)
            .first()
        )
        schedule = CollectionSchedule.objects.filter(region=region).first()
        timing = effective_forecast_schedule(
            first_slot=first_slot,
            schedule=schedule,
            daily_quota=binding.provider_account.daily_quota,
            cutoff=cutoff,
        )
        if timing is None:
            return jobs
        zone = ZoneInfo(region.timezone)
        today = now.astimezone(zone).date()
        window = nightly_window(today, region.timezone)
        if window is not None and window[0] <= now < window[1]:
            history = (
                CollectionJob.objects.filter(
                    binding=binding,
                    endpoint="estimated_actuals",
                    reference_import__isnull=True,
                    target_date=today - timedelta(days=1),
                    scheduled_slot__gte=window[0],
                    scheduled_slot__lt=window[1],
                    retry_until=window[1],
                )
                .order_by("scheduled_slot", "pk")
                .first()
            )
            if history is None:
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
        slot = latest_forecast_slot(now, region.timezone, timing.times, timing.effective_from)
        if slot is None:
            return jobs
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
        if now < slot + timedelta(minutes=15):
            history, _ = CollectionJob.objects.get_or_create(
                binding=binding,
                endpoint="estimated_actuals",
                scheduled_slot=slot,
                defaults={
                    "target_date": slot.astimezone(zone).date(),
                    "available_at": now,
                    "retry_until": slot + timedelta(minutes=15),
                },
            )
            jobs.append(history.pk)
    return jobs


def enqueue_due(now: datetime, cutoff_hour: int | None = None) -> list[int]:
    if now.tzinfo is None or (cutoff_hour is not None and not 0 <= cutoff_hour <= 23):
        raise ValidationError("An aware timestamp and valid cutoff hour are required.")
    cutoff: time = settings.PYRACAST_CUTOFF_LOCAL if cutoff_hour is None else time(cutoff_hour)
    jobs: list[int] = []
    failed_region_ids: list[int] = []
    for region_id in Region.objects.filter(status="approved", deleted_at__isnull=True).values_list(
        "pk", flat=True
    ):
        try:
            jobs.extend(_enqueue_region(region_id, now, cutoff))
        except Exception as exc:
            failed_region_ids.append(region_id)
            log_failure(logger, "collection_region_scheduling_failed", exc, region_id=region_id)
            close_old_connections()
    if failed_region_ids:
        raise CollectionSchedulingError(failed_region_ids)
    return jobs
