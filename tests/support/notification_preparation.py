from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pyracast.forecasts.models import (
    ForecastSnapshot,
)
from pyracast.irradiance import local_day_bounds
from tests.support.domain import NOW, System
from tests.support.notifications import (
    SLOT,
    saved,
)


def complete_saved(system: System, when: datetime = NOW) -> ForecastSnapshot:
    start, _ = local_day_bounds(
        SLOT.astimezone(ZoneInfo(system.region.timezone)).date(), system.binding.timezone
    )
    return saved(
        system,
        when,
        intervals=[
            {
                "start": (start + timedelta(hours=hour)).isoformat(),
                "end": (start + timedelta(hours=hour + 1)).isoformat(),
                "power_kw": 0.3,
                "sunlight": "day",
            }
            for hour in range(48)
        ],
    )
