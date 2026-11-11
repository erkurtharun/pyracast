from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pyracast.notifications.schedule import local_slot

HISTORY_CLOCK = "03:00"
HISTORY_END_CLOCK = "09:00"
NIGHT_RETRY_DELAY = timedelta(minutes=30)


def nightly_window(day: date, timezone_name: str) -> tuple[datetime, datetime] | None:
    zone = ZoneInfo(timezone_name)
    end = local_slot(day, HISTORY_END_CLOCK, zone)
    if end is None:
        return None
    first = time.fromisoformat(HISTORY_CLOCK)
    last = time.fromisoformat(HISTORY_END_CLOCK)
    for minutes in range(
        first.hour * 60 + first.minute,
        last.hour * 60 + last.minute,
        int(NIGHT_RETRY_DELAY.total_seconds() / 60),
    ):
        clock = f"{minutes // 60:02}:{minutes % 60:02}"
        start = local_slot(day, clock, zone)
        if start is not None:
            return start, end
    return None
