from datetime import timedelta

import pytest
from django.test import Client

from pyracast.connections.operations import reserve_request
from pyracast.forecasts.models import CollectionJob, ForecastSnapshot, RequestAttempt
from pyracast.forecasts.schedule import get_schedule, schedule_payload
from pyracast.regions.operations import configure_binding, submit_region
from tests.test_domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_collection_status_reports_the_plants_own_collection_only() -> None:
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
    for job, binding in ((original, system.binding), (other_job, other_binding)):
        ForecastSnapshot.objects.create(
            job=job,
            binding=binding,
            raw_payload={},
            normalized_intervals=[],
            collected_at=moment - timedelta(minutes=job.pk),
            source_kind="forecast",
        )
    payload = schedule_payload(schedule)
    assert payload["last_success_at"] == moment - timedelta(minutes=original.pk)
    original.refresh_from_db()
    assert payload["next_collection_at"] == min(original.available_at, moment)
    assert not {"used_today", "remaining_today", "requests_by_outcome", "quota_utc_day"} & set(
        payload
    )
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
    assert "next_collection_at" in response.json()
    assert "recent_jobs" in response.json()
