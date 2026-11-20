from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.connections.models import SiteBinding
from pyracast.connections.operations import rotate_secret
from pyracast.forecasts.jobs.claim import claim
from pyracast.forecasts.models import CollectionJob, RequestAttempt, RequestPurpose
from pyracast.forecasts.quota import RequestDeferredError, reserve_request
from pyracast.forecasts.schedule import configure_schedule
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.regions.operations import configure_binding, submit_region
from tests.support.domain import NOW, System, make_system

pytestmark = pytest.mark.django_db(transaction=True)
# Europe/Istanbul is UTC+3: its nightly history window of local day D runs from 21:00 UTC on
# D - 1 to 06:00 UTC on D.
EVENING = NOW + timedelta(hours=1)
WINDOW_START = datetime(2026, 11, 9, 21, tzinfo=UTC)
WINDOW_END = datetime(2026, 11, 10, 6, tzinfo=UTC)
NEXT_SLOT = datetime(2026, 11, 10, 13, tzinfo=UTC)
TICK = timedelta(microseconds=1)


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
