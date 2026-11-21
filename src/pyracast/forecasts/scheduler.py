import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.db.models.functions import Greatest

from pyracast.background.failures import log_failure
from pyracast.collection_window import INTRADAY_HISTORY_WINDOW, nightly_window
from pyracast.connections.currency import binding_is_current
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.jobs.ready import WAITING
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionInactiveReason,
    CollectionJob,
    CollectionJobError,
    CollectionJobStatus,
    CollectionSchedule,
)
from pyracast.forecasts.timing import ForecastTiming, forecast_timing, latest_forecast_slot
from pyracast.regions.models import Region, RegionStatus

logger = logging.getLogger(__name__)
# A stale forecast job at the latest slot is opened again unless it was refused for a reason that
# holds for its slot: the binding's day bound, or a schedule disabled after the claim.
SLOT_FINAL_ERRORS = (CollectionJobError.QUOTA_EXHAUSTED, CollectionJobError.SCHEDULE_DISABLED)
# A stale nightly history job ended by these refusals was skipped as planned: the provider day or
# the binding's day bound outlived its window (claim.retry), so nothing can change before it ends.
NIGHT_FINAL_ERRORS = (
    CollectionJobError.QUOTA_EXHAUSTED,
    CollectionJobError.HISTORY_BUDGET_RESERVED,
    CollectionJobError.ACCOUNT_UNAVAILABLE,
    CollectionJobError.PROVIDER_ALLOWANCE_EXHAUSTED,
)


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


@dataclass(frozen=True)
class NightWindow:
    """The open nightly history window of a local day, collecting the day before."""

    start: datetime
    end: datetime
    target: date

    def history(self) -> Q:
        return Q(
            endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
            reference_import__isnull=True,
            target_date=self.target,
            scheduled_for__gte=self.start,
            scheduled_for__lt=self.end,
            retry_until=self.end,
        )

    def revivable(self) -> Q:
        """A stale history job of the window that saved nothing, was not skipped for the day and
        can still be claimed before the window ends; the scheduler revives it."""
        return Q(
            endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
            target_date=self.target,
            scheduled_for__gte=self.start,
            retry_until=self.end,
            available_at__lt=self.end,
            status=CollectionJobStatus.STALE,
            forecastsnapshot__isnull=True,
        ) & ~Q(last_error__in=NIGHT_FINAL_ERRORS)


def open_night_window(now: datetime, timezone_name: str) -> NightWindow | None:
    today = now.astimezone(ZoneInfo(timezone_name)).date()
    window = nightly_window(today, timezone_name)
    if window is None or not window[0] <= now < window[1]:
        return None
    return NightWindow(window[0], window[1], today - timedelta(days=1))


@dataclass(frozen=True)
class Collecting:
    binding: SiteBinding
    timing: ForecastTiming


def collection_gate(
    region: Region, schedule: CollectionSchedule | None
) -> Collecting | CollectionInactiveReason:
    """The scheduler's gate, in its order, for a loaded region and its saved schedule."""
    binding = region.approved_binding
    if region.status != RegionStatus.APPROVED:
        return CollectionInactiveReason.NOT_APPROVED
    if binding is None or binding.validated_at is None:
        return CollectionInactiveReason.NOT_VALIDATED
    if not binding_is_current(binding, region):
        return CollectionInactiveReason.BINDING_NOT_CURRENT
    if schedule is not None and not schedule.enabled:
        return CollectionInactiveReason.SCHEDULE_DISABLED
    timing = forecast_timing(binding, schedule)
    # Approval creates the first forecast job; a binding without one was never approved.
    return CollectionInactiveReason.NOT_APPROVED if timing is None else Collecting(binding, timing)


def _schedule_night_history(context: _RegionContext) -> int | None:
    binding, now = context.binding, context.now
    window = open_night_window(now, context.timezone)
    if window is None:
        return None
    history = (
        CollectionJob.objects.filter(window.history(), binding=binding)
        .order_by("scheduled_for", "pk")
        .first()
    )
    if history is None:
        history, _ = CollectionJob.objects.get_or_create(
            binding=binding,
            endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
            scheduled_for=window.start,
            defaults={
                "target_date": window.target,
                "available_at": now,
                "retry_until": window.end,
            },
        )
    CollectionJob.objects.filter(window.revivable(), binding=binding).update(
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
        status__in=WAITING,
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
        gate = collection_gate(region, CollectionSchedule.objects.filter(region=region).first())
        if isinstance(gate, CollectionInactiveReason):
            return []
        context = _RegionContext(gate.binding, region.timezone, now)
        jobs = [pk for pk in (_schedule_night_history(context),) if pk is not None]
        timing = gate.timing
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
