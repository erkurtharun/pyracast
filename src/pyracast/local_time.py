import re
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

CLOCK_PATTERN = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def local_slot(day: date, clock: str, zone: ZoneInfo) -> datetime | None:
    local = datetime.combine(day, time.fromisoformat(clock), tzinfo=zone)
    slot = local.astimezone(UTC)
    # Nonexistent wall times are skipped; repeated times use their first occurrence.
    return (
        slot if slot.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None) else None
    )
