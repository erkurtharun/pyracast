from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings

from pyracast.forecasts.models import CollectionSchedule
from pyracast.notifications.schedule import local_slot


@dataclass(frozen=True)
class ForecastTiming:
    times: tuple[str, ...]
    effective_from: datetime


def effective_forecast_schedule(
    *,
    first_slot: datetime | None,
    schedule: CollectionSchedule | None,
    daily_quota: int,
    cutoff: time | None = None,
) -> ForecastTiming | None:
    if first_slot is None or (schedule is not None and not schedule.enabled):
        return None
    cutoff = settings.PYRACAST_CUTOFF_LOCAL if cutoff is None else cutoff
    times = schedule.times if schedule is not None else [cutoff.strftime("%H:%M")]
    effective_from = (
        max(first_slot, schedule.effective_from) if schedule is not None else first_slot
    )
    return ForecastTiming(tuple(times[: max(daily_quota - 1, 0)]), effective_from)


def latest_forecast_slot(
    now: datetime, timezone: str, times: Sequence[str], effective_from: datetime
) -> datetime | None:
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    return max(
        (
            slot
            for day in (today - timedelta(days=1), today)
            for clock in times
            if (slot := local_slot(day, clock, zone)) is not None and effective_from <= slot <= now
        ),
        default=None,
    )


def next_forecast_slot(
    now: datetime, timezone: str, times: Sequence[str], effective_from: datetime
) -> datetime | None:
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    return min(
        (
            slot
            for day in (today, today + timedelta(days=1))
            for clock in times
            if (slot := local_slot(day, clock, zone)) is not None
            and now < slot
            and effective_from <= slot
        ),
        default=None,
    )
