from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pyracast.notifications.schedule import local_slot


def latest_forecast_slot(
    now: datetime, timezone: str, times: list[str], effective_from: datetime
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
