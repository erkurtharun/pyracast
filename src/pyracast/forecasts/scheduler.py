import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import close_old_connections, transaction
from django.db.models.functions import Greatest

from pyracast.background.failures import log_failure
from pyracast.collection_window import INTRADAY_HISTORY_WINDOW, nightly_window
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
from pyracast.regions.models import Region

logger = logging.getLogger(__name__)
# A stale forecast job at the latest slot is opened again unless it was refused for a reason that
# holds for its slot: the binding's day bound, or a schedule disabled after the claim.
SLOT_FINAL_ERRORS = (CollectionJobError.QUOTA_EXHAUSTED, CollectionJobError.SCHEDULE_DISABLED)


class CollectionSchedulingError(RuntimeError):
    def __init__(self, failed_region_ids: list[int]) -> None:
        super().__init__("Collection scheduling failed for one or more regions.")
        self.failed_region_ids = failed_region_ids


@dataclass(frozen=True)
class _RegionContext:
    binding: SiteBinding
    timezone: str
    now: datetime

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _schedule_night_history(context: _RegionContext) -> int | None:
    binding, now = context.binding, context.now
    today = now.astimezone(context.zone).date()
    window = nightly_window(today, context.timezone)
    if window is None or not window[0] <= now < window[1]:
        return None
    history = (
        CollectionJob.objects.filter(
            binding=binding,
            endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
            reference_import__isnull=True,
            target_date=today - timedelta(days=1),
            scheduled_for__gte=window[0],
            scheduled_for__lt=window[1],
            retry_until=window[1],
        )
        .order_by("scheduled_for", "pk")
        .first()
    )
    if history is None:
        history, _ = CollectionJob.objects.get_or_create(
            binding=binding,
            endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
            scheduled_for=window[0],
            defaults={
                "target_date": today - timedelta(days=1),
                "available_at": now,
                "retry_until": window[1],
            },
        )
    CollectionJob.objects.filter(
        binding=binding,
        endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
        target_date=today - timedelta(days=1),
        scheduled_for__gte=window[0],
        retry_until=window[1],
        status=CollectionJobStatus.STALE,
        forecastsnapshot__isnull=True,
    ).update(
        status=CollectionJobStatus.PENDING,
        available_at=Greatest("available_at", now),
        last_error="",
        lease_token=None,
        lease_expires_at=None,
    )
    return history.pk


def _schedule_forecast_slot(context: _RegionContext, slot: datetime) -> int:
    binding, now = context.binding, context.now
    CollectionJob.objects.filter(
        binding=binding,
        endpoint=CollectionEndpoint.FORECASTS,
        scheduled_for__lt=slot,
        status__in=(CollectionJobStatus.PENDING, CollectionJobStatus.RETRY),
    ).update(
        status=CollectionJobStatus.STALE,
        last_error=CollectionJobError.FORECAST_SUPERSEDED,
        lease_token=None,
        lease_expires_at=None,
    )
    job, _ = CollectionJob.objects.get_or_create(
        binding=binding,
        endpoint=CollectionEndpoint.FORECASTS,
        scheduled_for=slot,
        defaults={
            "target_date": slot.astimezone(context.zone).date() + timedelta(days=1),
            "available_at": now,
        },
    )
    CollectionJob.objects.filter(pk=job.pk, status=CollectionJobStatus.STALE).exclude(
        last_error__in=SLOT_FINAL_ERRORS
    ).update(
        status=CollectionJobStatus.PENDING,
        available_at=now,
        last_error="",
        lease_token=None,
        lease_expires_at=None,
    )
    return job.pk


def _schedule_paired_actuals(context: _RegionContext, slot: datetime) -> int | None:
    if context.now >= slot + INTRADAY_HISTORY_WINDOW:
        return None
    history, _ = CollectionJob.objects.get_or_create(
        binding=context.binding,
        endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
        scheduled_for=slot,
        defaults={
            "target_date": slot.astimezone(context.zone).date(),
            "available_at": context.now,
            "retry_until": slot + INTRADAY_HISTORY_WINDOW,
        },
    )
    return history.pk


def _enqueue_region(region_id: int, now: datetime) -> list[int]:
    with transaction.atomic():
        region = (
            Region.objects.select_for_update(of=("self",))
            .select_related("approved_binding__provider_account")
            .filter(pk=region_id)
            .first()
        )
        if region is None:
            return []
        binding = region.approved_binding
        if binding is None or binding.validated_at is None or not binding_is_current(binding):
            return []
        timing = forecast_timing(binding, CollectionSchedule.objects.filter(region=region).first())
        if timing is None:
            return []
        context = _RegionContext(binding, region.timezone, now)
        jobs = [pk for pk in (_schedule_night_history(context),) if pk is not None]
        slot = latest_forecast_slot(now, region.timezone, timing.times, timing.effective_from)
        if slot is None:
            return jobs
        jobs.append(_schedule_forecast_slot(context, slot))
        paired = _schedule_paired_actuals(context, slot)
        return jobs if paired is None else [*jobs, paired]


def enqueue_due(now: datetime) -> list[int]:
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    jobs: list[int] = []
    failed_region_ids: list[int] = []
    for region_id in Region.objects.active().filter(status="approved").values_list("pk", flat=True):
        try:
            jobs.extend(_enqueue_region(region_id, now))
        except Exception as exc:
            failed_region_ids.append(region_id)
            log_failure(logger, "collection_region_scheduling_failed", exc, region_id=region_id)
            close_old_connections()
    if failed_region_ids:
        raise CollectionSchedulingError(failed_region_ids)
    return jobs
