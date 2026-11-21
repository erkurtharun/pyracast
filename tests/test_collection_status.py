from datetime import datetime, timedelta

import pytest
from django.test import Client

from pyracast.forecasts.models import (
    CollectionJob,
    ForecastSnapshot,
    HistoricalReferenceImport,
    RequestAttempt,
    RequestPurpose,
)
from pyracast.forecasts.quota import reserve_request
from pyracast.forecasts.schedule import configure_schedule, get_schedule, schedule_payload
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.plants.approval import ReviewDecision, decide_review
from pyracast.regions.operations import configure_binding, submit_region
from tests.support.domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_collection_status_reports_the_plants_own_collection_only() -> None:
    system = make_system(approved=True)
    moment = NOW.replace(hour=23, minute=30)
    schedule = get_schedule(system.tenant.pk, system.region.pk, moment)
    for outcome in ("succeeded", "failed", "uncertain"):
        attempt = reserve_request(system.binding, moment, purpose=RequestPurpose.FORECAST)
        RequestAttempt.objects.filter(pk=attempt.pk).update(outcome=outcome)
    reserve_request(system.binding, moment - timedelta(days=1), purpose=RequestPurpose.VALIDATION)
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
    other_attempt = reserve_request(other_binding, moment, purpose=RequestPurpose.FORECAST)
    RequestAttempt.objects.filter(pk=other_attempt.pk).update(outcome="failed")
    other_job = CollectionJob.objects.create(
        binding=other_binding,
        target_date=NOW.date() + timedelta(days=1),
        scheduled_for=moment + timedelta(hours=1),
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
            scheduled_for=moment + timedelta(minutes=offset),
            available_at=moment,
        )
    for job, binding in ((original, system.binding), (other_job, other_binding)):
        collected_job = CollectionJob.objects.create(
            binding=binding,
            target_date=job.target_date,
            scheduled_for=job.scheduled_for - timedelta(days=1),
            available_at=moment - timedelta(days=1),
            status="succeeded",
        )
        ForecastSnapshot.objects.create(
            job=collected_job,
            binding=binding,
            raw_payload={},
            normalized_intervals=[],
            collected_at=moment - timedelta(minutes=job.pk) if job is original else moment,
            source_kind="forecast",
        )
    payload = schedule_payload(schedule, moment)
    assert payload["last_success_at"] == moment - timedelta(minutes=original.pk)
    original.refresh_from_db()
    assert payload["next_collection_at"] == min(original.available_at, moment)
    assert not {"used_today", "remaining_today", "requests_by_outcome", "quota_utc_day"} & set(
        payload
    )
    jobs = payload["recent_jobs"]
    assert isinstance(jobs, list)
    assert len(jobs) == 6
    assert jobs[0]["scheduled_for"] == moment + timedelta(minutes=6)
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


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize(
    "endpoint", ["historical_import", "scaled_forecasts", "scaled_estimated_actuals"]
)
def test_last_success_reports_native_collection_independently_of_operator_imports(
    endpoint: str,
    native: bool,
) -> None:
    system = make_system(approved=True)
    if native:
        job = CollectionJob.objects.get(binding=system.binding)
        CollectionJob.objects.filter(pk=job.pk).update(status="succeeded")
        ForecastSnapshot.objects.create(
            job=job,
            binding=system.binding,
            raw_payload={},
            normalized_intervals=[],
            collected_at=NOW,
            source_kind="forecast",
        )
    reference = (
        HistoricalReferenceImport.objects.create(
            binding=system.binding,
            content_sha256="a" * 64,
            source_name="authorized-export.json",
            source_settings={},
            imported_by=system.admin,
            created_at=NOW,
            interval_count=1,
        )
        if endpoint == "historical_import"
        else None
    )
    imported = CollectionJob.objects.create(
        binding=system.binding,
        reference_import=reference,
        endpoint=endpoint,
        target_date=NOW.date(),
        scheduled_for=NOW + timedelta(hours=1),
        available_at=NOW + timedelta(hours=1),
        status="succeeded",
    )
    ForecastSnapshot.objects.create(
        job=imported,
        binding=system.binding,
        raw_payload={},
        normalized_intervals=[],
        collected_at=NOW + timedelta(hours=1),
        source_kind="estimated_actuals" if endpoint != "scaled_forecasts" else "forecast",
    )
    schedule = get_schedule(system.tenant.pk, system.region.pk, NOW)

    assert schedule_payload(schedule, NOW)["last_success_at"] == (NOW if native else None)
    assert ForecastSnapshot.objects.filter(binding=system.binding).count() == (2 if native else 1)


@pytest.mark.parametrize("stop", ["disable_schedule", "suspend_plant"])
def test_next_collection_is_the_earliest_queued_job_forecast_slot_or_nightly_window(
    stop: str,
) -> None:
    system = make_system(approved=True)
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["15:00", "09:00"],
        now=NOW,
    )
    morning = (NOW + timedelta(days=1)).replace(hour=6)
    enqueue_due(morning)
    CollectionJob.objects.filter(status="pending").update(status="succeeded")

    def next_collection(now: datetime) -> object:
        schedule = get_schedule(system.tenant.pk, system.region.pk, now)
        return schedule_payload(schedule, now)["next_collection_at"]

    later = morning + timedelta(hours=1)
    assert next_collection(later) == morning + timedelta(hours=6)
    assert next_collection(morning + timedelta(hours=7)) == morning + timedelta(hours=15)
    retry_at = later + timedelta(minutes=30)
    CollectionJob.objects.filter(endpoint="forecasts", scheduled_for=morning).update(
        status="retry", available_at=retry_at
    )
    assert next_collection(later) == retry_at
    if stop == "disable_schedule":
        configure_schedule(
            system.member, system.tenant.pk, system.region.pk, enabled=False, times=[], now=later
        )
    else:
        decide_review(system.admin, system.region.pk, ReviewDecision.SUSPEND, reason="Paused.")
    assert next_collection(later) is None
