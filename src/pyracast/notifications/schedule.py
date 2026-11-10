from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo


def local_slot(day: date, clock: str, zone: ZoneInfo) -> datetime | None:
    local = datetime.combine(day, time.fromisoformat(clock), tzinfo=zone)
    slot = local.astimezone(UTC)
    # Nonexistent wall times are skipped; repeated times use their first occurrence.
    return (
        slot if slot.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None) else None
    )
