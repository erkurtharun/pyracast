from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
)
from tests.support.domain import System

NOW = datetime(2026, 11, 12, 13, tzinfo=UTC)


def recent_forecast(system: System) -> ForecastSnapshot:
    job = CollectionJob.objects.get(binding=system.binding)
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


class _EmptyQueues:
    def queue_declare(self, queue: str, passive: bool = False) -> SimpleNamespace:
        return SimpleNamespace(method=SimpleNamespace(message_count=0, consumer_count=1))


class AnsweringBroker:
    channel = _EmptyQueues()

    def close(self) -> None:
        pass


def answering_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyracast.background.broker.make_broker", AnsweringBroker)
