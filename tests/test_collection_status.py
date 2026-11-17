from datetime import UTC, timedelta

import pytest
from django.test import Client

from pyracast.connections.operations import reserve_request
from pyracast.forecasts.models import CollectionJob, RequestAttempt
from pyracast.forecasts.schedule import get_schedule, schedule_payload
from pyracast.regions.operations import configure_binding, submit_region
from tests.support.domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_collection_status_counts_account_attempts_by_utc_day_without_leaking_other_jobs() -> None:
    system = make_system(approved=True)
    moment = NOW.replace(hour=23, minute=30)
    schedule = get_schedule(system.member, system.tenant.pk, system.region.pk, moment)
    for outcome in ("succeeded", "failed", "uncertain"):
        attempt = reserve_request(system.binding, moment, purpose="forecast")
        RequestAttempt.objects.filter(pk=attempt.pk).update(outcome=outcome)
    reserve_request(system.binding, moment - timedelta(days=1), purpose="validation")
    other_region = submit_region(
        system.member, system.tenant.pk, name="Other", latitude=41, longitude=29, timezone="UTC"
    )
    other_binding = configure_binding(
        system.admin,
        other_region.pk,
        provider_account_id=system.account.pk,
        site_id="other",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    other_attempt = reserve_request(other_binding, moment, purpose="forecast")
    RequestAttempt.objects.filter(pk=other_attempt.pk).update(outcome="failed")
    other_job = CollectionJob.objects.create(
        binding=other_binding,
        target_date=NOW.date() + timedelta(days=1),
        scheduled_slot=moment + timedelta(hours=1),
        available_at=moment,
    )
    original = CollectionJob.objects.get(binding=system.binding)
    CollectionJob.objects.filter(pk=original.pk).update(
        status="retry", last_error="provider_failure"
    )
    for offset in range(7):
        CollectionJob.objects.create(
            binding=system.binding,
            target_date=NOW.date() + timedelta(days=offset + 1),
            scheduled_slot=moment + timedelta(minutes=offset),
            available_at=moment,
        )
    payload = schedule_payload(schedule, moment)
    assert payload["quota_utc_day"] == moment.astimezone(UTC).date().isoformat()
    assert payload["requests_by_outcome"] == {"succeeded": 2, "failed": 2, "uncertain": 1}
    assert payload["used_today"] == 5
    assert payload["remaining_today"] == 5
    jobs = payload["recent_jobs"]
    assert isinstance(jobs, list)
    assert len(jobs) == 6
    assert jobs[0]["scheduled_slot"] == moment + timedelta(minutes=6)
    assert all(row["id"] != original.pk for row in jobs)
    assert all(row["id"] != other_job.pk for row in jobs)
    client = Client()
    client.force_login(system.member)
    response = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/collection-schedule/"
    )
    assert response.status_code == 200
    assert "quota_utc_day" in response.json()
    assert "recent_jobs" in response.json()


def test_collection_status_resets_at_utc_midnight() -> None:
    system = make_system(approved=True)
    after_midnight = NOW.replace(hour=0, minute=1) + timedelta(days=1)
    schedule = get_schedule(system.member, system.tenant.pk, system.region.pk, after_midnight)
    payload = schedule_payload(schedule, after_midnight)
    assert payload["quota_utc_day"] == after_midnight.date().isoformat()
    assert payload["used_today"] == 0
    assert payload["requests_by_outcome"] == {"succeeded": 0, "failed": 0, "uncertain": 0}
    assert payload["remaining_today"] == 10
