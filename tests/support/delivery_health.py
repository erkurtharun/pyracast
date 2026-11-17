from datetime import UTC, datetime

from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
)
from tests.support.domain import System

NOW = datetime(2026, 11, 12, 13, tzinfo=UTC)


def recent_forecast(system: System) -> ForecastSnapshot:
    job = CollectionJob.objects.get()
    job.status = "succeeded"
    job.save(update_fields=("status",))
    snapshot = ForecastSnapshot.objects.create(
        job=job,
        binding=system.binding,
        raw_payload={},
        normalized_intervals=[],
        source_kind="forecast",
        collected_at=NOW,
        available_at=NOW,
    )
    ForecastResult.objects.create(snapshot=snapshot, payload={}, published_at=NOW)
    return snapshot
