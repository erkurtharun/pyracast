from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import SiteBinding
from pyracast.connections.operations import rotate_secret
from pyracast.forecasts.jobs.claim import claim
from pyracast.forecasts.jobs.ready import pending_job_ids
from pyracast.forecasts.models import CollectionJob, RequestAttempt, RequestPurpose
from pyracast.forecasts.quota import RequestDeferredError, reserve_request
from pyracast.forecasts.schedule import configure_schedule
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.regions.operations import configure_binding, submit_region
from tests.support.collection_schedule import HistoricalFixture, no_calculation
from tests.support.domain import NOW, System, make_system
from tests.support.jobs import collect

pytestmark = pytest.mark.django_db(transaction=True)
# Europe/Istanbul is UTC+3: its nightly history window of local day D runs from 21:00 UTC on
# D - 1 to 06:00 UTC on D.
EVENING = NOW + timedelta(hours=1)
WINDOW_START = datetime(2026, 11, 9, 21, tzinfo=UTC)
WINDOW_END = datetime(2026, 11, 10, 6, tzinfo=UTC)
NIGHT = datetime(2026, 11, 10, tzinfo=UTC)
NEXT_SLOT = datetime(2026, 11, 10, 13, tzinfo=UTC)
TICK = timedelta(microseconds=1)
# claim reads its clock again after taking the locks, so a claim one tick early can reach the time.
EARLY = timedelta(seconds=1)


def read(system: System, now: datetime, user: User | None = None) -> dict[str, Any]:
    client = Client()
    client.force_login(user or system.member)
    with patch.object(timezone, "now", lambda: now):
        response = client.get(
            f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/collection-schedule/"
        )
    assert response.status_code == 200, response.content
    payload: dict[str, Any] = response.json()
    return payload


def at(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def collected(system: System) -> System:
    CollectionJob.objects.filter(binding=system.binding).update(status="succeeded")
    return system


def created_jobs(now: datetime) -> set[int]:
    before = set(CollectionJob.objects.values_list("pk", flat=True))
    enqueue_due(now)
    return set(CollectionJob.objects.values_list("pk", flat=True)) - before


def test_a_collecting_plant_names_the_moment_the_scheduler_acts_next() -> None:
    system = collected(make_system(approved=True))

    payload = read(system, EVENING)

    assert (payload["state"], payload["inactive_reason"]) == ("collecting", None)
    assert at(payload["next_collection_at"]) == WINDOW_START
    assert created_jobs(WINDOW_START - TICK) == set()
    assert len(created_jobs(WINDOW_START)) == 1


@pytest.mark.parametrize(
    ("setup", "state", "reason"),
    [
        ("pending", "waiting_for_approval", "not_approved"),
        ("suspended", "paused", "not_approved"),
        ("unvalidated", "paused", "not_validated"),
        ("rotated", "paused", "binding_not_current"),
        ("disabled", "paused", "schedule_disabled"),
    ],
)
def test_a_plant_the_scheduler_skips_says_why_and_has_no_next_collection(
    setup: str, state: str, reason: str
) -> None:
    system = make_system(approved=setup != "pending")
    if setup == "suspended":
        system.region.status = "suspended"
        system.region.save(update_fields=("status",))
    elif setup == "unvalidated":
        SiteBinding.objects.filter(pk=system.binding.pk).update(
            validated_at=None, validated_credential_revision=None
        )
    elif setup == "rotated":
        rotate_secret(system.admin, system.account.pk, "test-only-rotated-state-key")
    elif setup == "disabled":
        configure_schedule(
            system.member, system.tenant.pk, system.region.pk, enabled=False, times=[], now=NOW
        )

    payload = read(system, EVENING)

    assert (payload["state"], payload["inactive_reason"]) == (state, reason)
    assert payload["next_collection_at"] is None
    assert created_jobs(WINDOW_START) == set()
    assert created_jobs(NEXT_SLOT) == set()


def test_a_quota_of_one_collects_history_only() -> None:
    system = collected(make_system(approved=True))
    system.account.daily_quota = 1
    system.account.save(update_fields=("daily_quota",))

    payload = read(system, EVENING)

    assert (payload["state"], payload["inactive_reason"]) == ("collecting", "quota_too_low")
    assert at(payload["next_collection_at"]) == WINDOW_START
    assert payload["max_forecast_times"] == 0
    history = created_jobs(WINDOW_START)
    assert CollectionJob.objects.get(pk__in=history).endpoint == "estimated_actuals"
    assert created_jobs(NEXT_SLOT) == set()


@pytest.mark.parametrize("cause", ["cooldown", "rejected_key"])
def test_a_provider_that_refuses_requests_needs_attention(cause: str) -> None:
    system = collected(make_system(approved=True))
    if cause == "cooldown":
        system.account.request_blocked_until = EVENING + timedelta(hours=2)
    else:
        system.account.authentication_failed_revision = system.account.credential_revision
    system.account.save()

    payload = read(system, EVENING)

    assert (payload["state"], payload["inactive_reason"]) == (
        "provider_attention",
        "provider_unavailable",
    )
    with pytest.raises(RequestDeferredError):
        reserve_request(system.binding, EVENING, purpose=RequestPurpose.FORECAST)


@pytest.mark.parametrize(
    "moment", [WINDOW_START, WINDOW_START + timedelta(hours=4), WINDOW_END - TICK]
)
def test_inside_an_open_night_window_a_missing_history_job_is_due_now(moment: datetime) -> None:
    system = collected(make_system(approved=True))

    assert at(read(system, moment)["next_collection_at"]) == moment
    created = created_jobs(moment)
    history = CollectionJob.objects.get(pk__in=created)
    assert (history.endpoint, history.scheduled_for) == ("estimated_actuals", WINDOW_START)

    CollectionJob.objects.filter(pk=history.pk).update(status="succeeded")
    assert at(read(system, moment)["next_collection_at"]) == NEXT_SLOT


def test_after_the_night_window_a_missing_history_job_is_not_due() -> None:
    system = collected(make_system(approved=True))

    assert at(read(system, WINDOW_END)["next_collection_at"]) == NEXT_SLOT
    assert created_jobs(WINDOW_END) == set()


def test_inside_an_open_night_window_a_revivable_history_job_is_due_now() -> None:
    system = collected(make_system(approved=True))
    moment = WINDOW_START + timedelta(hours=2)
    history = CollectionJob.objects.get(pk__in=created_jobs(WINDOW_START))
    CollectionJob.objects.filter(pk=history.pk).update(
        status="stale", last_error="schedule_changed"
    )

    assert at(read(system, moment)["next_collection_at"]) == moment
    enqueue_due(moment)
    history.refresh_from_db()
    assert history.status == "pending"


def test_a_queued_job_whose_window_closed_is_not_the_next_collection() -> None:
    system = collected(make_system(approved=True))
    retry_at = WINDOW_END - timedelta(hours=1)
    history = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="estimated_actuals",
        target_date=datetime(2026, 11, 9).date(),
        scheduled_for=WINDOW_START,
        available_at=retry_at,
        retry_until=WINDOW_END,
        status="retry",
        attempts=1,
    )

    assert at(read(system, WINDOW_END - TICK)["next_collection_at"]) == retry_at
    late = WINDOW_END + timedelta(hours=1)
    assert at(read(system, late)["next_collection_at"]) == NEXT_SLOT
    assert claim(history.pk, late) is None
    history.refresh_from_db()
    assert (history.status, history.last_error) == ("failed", "history_window_closed")


@pytest.mark.parametrize(
    "available_at",
    [WINDOW_END - timedelta(hours=1), WINDOW_END + timedelta(hours=1)],
    ids=["before-the-window-end", "after-the-window-end"],
)
def test_a_throttled_job_whose_cooldown_was_cleared_is_due_now(available_at: datetime) -> None:
    system = collected(make_system(approved=True))
    moment = WINDOW_START + timedelta(hours=2)
    history = CollectionJob.objects.get(pk__in=created_jobs(WINDOW_START))
    CollectionJob.objects.filter(pk=history.pk).update(
        status="retry", last_error="provider_throttled", available_at=available_at, attempts=1
    )

    assert at(read(system, moment)["next_collection_at"]) == moment
    assert claim(history.pk, moment) is not None


@pytest.mark.parametrize("error", ["quota_exhausted", "schedule_disabled"])
def test_a_forecast_ended_for_its_slot_is_not_collected_again_before_the_next_moment(
    error: str,
) -> None:
    system = make_system(approved=True)
    CollectionJob.objects.filter(binding=system.binding).update(status="stale", last_error=error)
    latest = CollectionJob.objects.get(binding=system.binding)

    assert at(read(system, EVENING)["next_collection_at"]) == WINDOW_START
    assert created_jobs(EVENING) == set()
    latest.refresh_from_db()
    assert (latest.status, latest.last_error) == ("stale", error)
    assert at(read(system, EVENING)["next_collection_at"]) == WINDOW_START


def test_a_forecast_ended_for_a_passing_reason_is_collected_again_now() -> None:
    system = make_system(approved=True)
    CollectionJob.objects.filter(binding=system.binding).update(
        status="stale", last_error="binding_not_current"
    )
    latest = CollectionJob.objects.get(binding=system.binding)

    assert at(read(system, EVENING)["next_collection_at"]) == EVENING
    enqueue_due(EVENING)
    latest.refresh_from_db()
    assert (latest.status, latest.available_at) == ("pending", EVENING)
    assert claim(latest.pk, EVENING) is not None


def night_history(system: System) -> CollectionJob:
    return CollectionJob.objects.get(
        binding=system.binding,
        endpoint="estimated_actuals",
        scheduled_for=WINDOW_START,
        retry_until=WINDOW_END,
    )


def test_reenabled_collection_names_a_partial_history_retry_at_its_own_wait() -> None:
    system = collected(make_system(approved=True))
    enqueue_due(NIGHT)
    parent = night_history(system)

    class Empty(HistoricalFixture):
        def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
            return ProviderData({}, (), collected_at=NIGHT, source_kind="estimated_actuals")

    assert collect(parent.pk, Empty(NIGHT), no_calculation, NIGHT)
    retry = CollectionJob.objects.get(binding=system.binding, status="pending")
    waits_until = retry.available_at
    assert waits_until >= NIGHT + timedelta(minutes=30)
    reenabled = NIGHT + timedelta(minutes=15)
    for enabled, moment in ((False, NIGHT + timedelta(minutes=10)), (True, reenabled)):
        configure_schedule(
            system.member,
            system.tenant.pk,
            system.region.pk,
            enabled=enabled,
            times=["16:00"] if enabled else [],
            now=moment,
        )

    assert at(read(system, reenabled)["next_collection_at"]) == waits_until
    enqueue_due(reenabled)
    retry.refresh_from_db()
    assert (retry.status, retry.available_at) == ("pending", waits_until)
    assert at(read(system, reenabled)["next_collection_at"]) == waits_until
    assert claim(retry.pk, waits_until - EARLY) is None
    assert claim(retry.pk, waits_until) is not None


@pytest.mark.parametrize(
    "moment",
    [NIGHT + timedelta(hours=1), WINDOW_END - timedelta(minutes=10)],
    ids=["inside-the-window", "at-the-window-end"],
)
def test_a_history_job_skipped_for_the_provider_day_is_not_collected_again_in_its_window(
    moment: datetime,
) -> None:
    system = collected(make_system(approved=True))
    enqueue_due(WINDOW_START)
    history = night_history(system)
    system.account.refresh_from_db()
    for _ in range(system.account.daily_quota):
        RequestAttempt.objects.create(
            provider_account=system.account,
            utc_day=moment.date(),
            created_at=moment,
            purpose=RequestPurpose.FORECAST,
        )
    assert not collect(history.pk, HistoricalFixture(moment), no_calculation, moment)
    history.refresh_from_db()
    assert (history.status, history.last_error) == ("stale", "quota_exhausted")
    later = moment + timedelta(minutes=1)

    assert at(read(system, later)["next_collection_at"]) == NEXT_SLOT
    assert created_jobs(later) == set()
    history.refresh_from_db()
    assert (history.status, history.last_error) == ("stale", "quota_exhausted")
    assert at(read(system, later)["next_collection_at"]) == NEXT_SLOT
    assert claim(history.pk, history.available_at) is None


def test_a_history_job_that_could_only_be_claimed_after_its_window_is_not_opened_again() -> None:
    system = collected(make_system(approved=True))
    enqueue_due(WINDOW_START)
    history = night_history(system)
    late = WINDOW_END - timedelta(minutes=10)
    CollectionJob.objects.filter(pk=history.pk).update(
        status="stale", last_error="schedule_changed", available_at=late + timedelta(minutes=30)
    )

    assert at(read(system, late)["next_collection_at"]) == NEXT_SLOT
    assert created_jobs(late) == set()
    history.refresh_from_db()
    assert (history.status, history.last_error) == ("stale", "schedule_changed")


def test_a_forecast_job_whose_lease_ended_is_collected_now() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get(binding=system.binding)
    assert claim(job.pk, EVENING) is not None
    job.refresh_from_db()
    assert job.lease_expires_at is not None
    later = job.lease_expires_at + timedelta(minutes=10)

    assert at(read(system, later)["next_collection_at"]) == later
    assert job.pk in pending_job_ids(later)
    assert claim(job.pk, later) is not None


def test_an_interrupted_history_job_is_collected_at_its_retry_time() -> None:
    system = collected(make_system(approved=True))
    enqueue_due(WINDOW_START)
    history = night_history(system)
    assert claim(history.pk, NIGHT) is not None
    history.refresh_from_db()
    assert history.lease_expires_at is not None
    retry_at = history.available_at
    assert history.lease_expires_at < retry_at

    assert at(read(system, history.lease_expires_at)["next_collection_at"]) == retry_at
    assert history.pk not in pending_job_ids(retry_at - TICK)
    assert claim(history.pk, retry_at - EARLY) is None
    assert claim(history.pk, retry_at) is not None


def test_usage_counts_only_the_plants_own_requests_of_the_utc_day() -> None:
    system = collected(make_system(approved=True))
    other_region = submit_region(
        system.member, system.tenant.pk, name="Other", latitude=41, longitude=29, timezone="UTC"
    )
    other = configure_binding(
        system.admin,
        other_region.pk,
        provider_account_id=system.account.pk,
        site_id="other",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    validation = RequestAttempt.objects.get(binding=system.binding)
    assert (validation.purpose, validation.utc_day) == ("validation", EVENING.date())
    for binding, moment, count in (
        (system.binding, EVENING, 2),
        (other, EVENING, 3),
        (system.binding, EVENING - timedelta(days=1), 1),
    ):
        for _ in range(count):
            reserve_request(binding, moment, purpose=RequestPurpose.FORECAST)

    assert read(system, EVENING)["requests_today"] == 3


def test_members_read_the_state_and_other_workspaces_learn_nothing() -> None:
    system = collected(make_system(approved=True))
    viewer = User.objects.create(username="viewer")
    Membership.objects.create(user=viewer, tenant=system.tenant, role=WorkspaceRole.VIEWER)
    assert read(system, EVENING, viewer)["state"] == "collecting"
    assert read(system, EVENING, system.member)["state"] == "collecting"

    outsider = User.objects.create(username="outsider")
    other = Tenant.objects.create(name="Other workspace")
    Membership.objects.create(user=outsider, tenant=other, role=WorkspaceRole.ADMIN)
    client = Client()
    client.force_login(outsider)

    def answer(tenant_id: int, region_id: int) -> tuple[int, bytes]:
        response = client.get(f"/api/tenants/{tenant_id}/regions/{region_id}/collection-schedule/")
        return response.status_code, response.content

    missing = system.region.pk + 1000
    assert answer(system.tenant.pk, system.region.pk) == answer(system.tenant.pk, missing)
    assert answer(system.tenant.pk, system.region.pk)[0] == 403
    assert answer(other.pk, system.region.pk) == answer(other.pk, missing)
    assert answer(other.pk, system.region.pk)[0] == 404
