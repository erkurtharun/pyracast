import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from pyracast.forecasts.jobs import claim
from pyracast.forecasts.models import CollectionJob, ForecastSnapshot, RequestAttempt
from tests.test_domain import NOW, make_system


@pytest.mark.django_db(transaction=True)
def test_upgrade_repairs_only_exhausted_quota_waits_without_external_or_snapshot_evidence() -> None:
    system = make_system(approved=True)
    original = CollectionJob.objects.get()
    previous = [("forecasts", "0018_dispatcher_status")]
    executor = MigrationExecutor(connection)
    current = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(previous)
        CollectionJob.objects.filter(pk=original.pk).update(
            status="retry", attempts=5, last_error="quota_exhausted"
        )
        preserved: list[tuple[int, int]] = []
        options: list[dict[str, object]] = [
            {"status": "pending"},
            {"status": "failed"},
            {"status": "succeeded"},
            {"last_error": "provider_failure"},
            {"attempts": 2},
            {"endpoint": "estimated_actuals"},
            {"lease_token": uuid.uuid4()},
            {"lease_expires_at": NOW + timedelta(minutes=5)},
            {},
            {},
        ]
        for index, overrides in enumerate(options, start=1):
            values: dict[str, object] = {
                "binding": system.binding,
                "scheduled_slot": NOW + timedelta(seconds=index),
                "target_date": original.target_date,
                "available_at": NOW,
                "status": "retry",
                "attempts": 5,
                "last_error": "quota_exhausted",
            }
            values.update(overrides)
            job = CollectionJob.objects.create(**values)
            if index == 9:
                RequestAttempt.objects.create(
                    provider_account=system.account,
                    binding=system.binding,
                    job=job,
                    utc_day=NOW.date(),
                    created_at=NOW,
                    purpose="forecast",
                    outcome="uncertain",
                )
            if index == 10:
                ForecastSnapshot.objects.create(
                    job=job,
                    binding=system.binding,
                    raw_payload={},
                    normalized_intervals=[],
                    collected_at=NOW,
                )
            preserved.append((job.pk, job.attempts))
        MigrationExecutor(connection).migrate(current)
        original.refresh_from_db()
        assert (original.status, original.attempts, original.last_error) == (
            "retry",
            0,
            "quota_exhausted",
        )
        assert claim(original.pk, NOW) is not None
        for identifier, attempts in preserved:
            assert CollectionJob.objects.get(pk=identifier).attempts == attempts
    finally:
        MigrationExecutor(connection).migrate(current)
