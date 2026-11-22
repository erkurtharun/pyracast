import itertools
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from django.db import close_old_connections, connection

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.budget import remaining_forecast_requests
from pyracast.forecasts.jobs.ready import pending_job_ids
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionJobError,
    CollectionSchedule,
    ForecastSnapshot,
    RequestAttempt,
    RequestPurpose,
)
from pyracast.forecasts.quota import (
    BindingDayExhaustedError,
    QuotaExceededError,
    ScheduleDisabledError,
    reserve_request,
)
from pyracast.forecasts.schedule import configure_schedule
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.provider_retry import next_provider_day
from pyracast.regions.models import Region
from tests.support.budget_reference import reference_forecast_requests
from tests.support.collection_schedule import HistoricalFixture
from tests.support.domain import NOW, FakeAdapter, System, make_system
from tests.support.jobs import calculate, collect

pytestmark = pytest.mark.django_db(transaction=True)

ISTANBUL = ZoneInfo("Europe/Istanbul")
SCHEDULES: tuple[tuple[bool, tuple[str, ...], timedelta] | None, ...] = (
    None,
    (True, ("01:00", "16:00"), timedelta(0)),
    (True, ("06:00", "13:00", "21:00"), timedelta(hours=-2)),
    (False, ("16:00",), timedelta(0)),
    (True, ("18:00", "23:30"), timedelta(hours=3)),
)
MOMENTS = (
    NOW - timedelta(hours=10),
    NOW,
    NOW + timedelta(hours=8),
    NOW + timedelta(hours=11),
)


@dataclass(frozen=True)
class JobState:
    """The planned job at a moment: an attempt made then or `attempted` earlier, a stored
    answer, a lease ending `lease` after the moment, available only after the UTC day."""

    status: str
    last_error: str = ""
    attempt: str | None = None
    attempted: timedelta = timedelta(0)
    answered: bool = False
    lease: timedelta | None = None
    late: bool = False
    blocked: bool = False


HELD, ENDED_LEASE = timedelta(minutes=1), timedelta(minutes=-1)
JOB_STATES = (
    JobState("pending"),
    JobState("retry", attempt="uncertain"),
    JobState("failed"),
    JobState("succeeded", attempt="uncertain"),
    JobState("retry", "provider_server_error", attempt="failed"),
    JobState("retry", "publication_failure", attempt="succeeded", answered=True),
    JobState("retry", "provider_throttled", attempt="failed", late=True, blocked=True),
    JobState("retry", "provider_throttled", attempt="failed", late=True),
    JobState("failed", "provider_authentication_failed", attempt="failed"),
    JobState("stale", "binding_not_current", attempt="failed"),
    JobState("running", lease=HELD),
    JobState("running", attempt="uncertain", lease=HELD),
    JobState("running", attempt="uncertain", attempted=timedelta(minutes=-10), lease=HELD),
    JobState("running", attempt="uncertain", lease=ENDED_LEASE),
)


def arrange_planned_job(
    system: System, planned: CollectionJob, state: JobState, moment: datetime
) -> None:
    ProviderAccount.objects.filter(pk=system.account.pk).update(
        request_blocked_until=moment + timedelta(days=2) if state.blocked else None
    )
    ForecastSnapshot.objects.filter(job=planned).delete()
    RequestAttempt.objects.filter(job=planned).delete()
    CollectionJob.objects.filter(pk=planned.pk).update(
        status=state.status,
        last_error=state.last_error,
        available_at=next_provider_day(moment) + timedelta(hours=1)
        if state.late
        else planned.scheduled_for,
        lease_token=None if state.lease is None else uuid.uuid4(),
        lease_expires_at=None if state.lease is None else moment + state.lease,
    )
    if state.attempt is not None:
        made = moment + state.attempted
        RequestAttempt.objects.create(
            provider_account=system.account,
            binding=system.binding,
            job=planned,
            utc_day=made.date(),
            created_at=made,
            purpose="forecast",
            outcome=state.attempt,
        )
    if state.answered:
        ForecastSnapshot.objects.create(
            job=planned,
            binding=system.binding,
            raw_payload={},
            normalized_intervals=[],
            collected_at=moment,
            source_kind="forecast",
        )


def second_binding(system: System) -> SiteBinding:
    other = Region.objects.create(
        tenant=system.tenant, name="Other", latitude=41, longitude=29, timezone="Europe/Istanbul"
    )
    binding = SiteBinding.objects.create(
        region=other,
        provider_account=system.account,
        region_revision=1,
        site_id="other",
        capacity_kw=1,
        latitude=41,
        longitude=29,
        timezone=other.timezone,
        validated_at=NOW,
        validated_credential_revision=1,
    )
    Region.objects.filter(pk=other.pk).update(status="approved", approved_binding=binding)
    CollectionJob.objects.create(
        binding=binding,
        endpoint="forecasts",
        scheduled_for=NOW,
        target_date=NOW.date() + timedelta(days=1),
        available_at=NOW,
    )
    return binding


def test_the_forecast_budget_equals_its_reference_across_schedules_and_job_states() -> None:
    system = make_system(approved=True)
    second_binding(system)
    slot = datetime(2026, 11, 9, 18, 0, tzinfo=UTC)
    planned = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="forecasts",
        scheduled_for=slot,
        target_date=slot.date() + timedelta(days=1),
        available_at=slot,
    )
    compared = 0
    for schedule, quota, state, moment in itertools.product(
        SCHEDULES, (2, 3, 10), JOB_STATES, MOMENTS
    ):
        CollectionSchedule.objects.filter(region=system.region).delete()
        if schedule is not None:
            enabled, times, offset = schedule
            CollectionSchedule.objects.create(
                region=system.region,
                enabled=enabled,
                times=list(times),
                effective_from=NOW + offset,
            )
        ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=quota)
        arrange_planned_job(system, planned, state, moment)
        account = ProviderAccount.objects.get(pk=system.account.pk)
        assert remaining_forecast_requests(account, moment) == reference_forecast_requests(
            account, moment
        ), (schedule, quota, state, moment)
        compared += 1
    assert compared == 840


def jobs_created(moments: list[datetime]) -> set[tuple[str, datetime, object, datetime | None]]:
    created: set[tuple[str, datetime, object, datetime | None]] = set()
    for moment in moments:
        enqueue_due(moment)
    for job in CollectionJob.objects.exclude(scheduled_for=NOW):
        created.add((job.endpoint, job.scheduled_for, job.target_date, job.retry_until))
    return created


@pytest.mark.parametrize("times", [None, ["06:00", "16:00"]])
def test_next_day_jobs_of_an_untouched_plant_keep_their_instants(times: list[str] | None) -> None:
    system = make_system(approved=True)
    if times is not None:
        CollectionSchedule.objects.create(
            region=system.region, times=times, effective_from=NOW - timedelta(days=3)
        )
    moments = [NOW + timedelta(minutes=10 * step) for step in range(1, 6 * 30)]

    created = jobs_created(moments)

    night = datetime(2026, 11, 9, 21, tzinfo=UTC)
    forecast = datetime(2026, 11, 10, 13, tzinfo=UTC)
    expected = {
        ("estimated_actuals", night, night.date(), night + timedelta(hours=9)),
        ("forecasts", forecast, forecast.date() + timedelta(days=1), None),
        ("estimated_actuals", forecast, forecast.date(), forecast + timedelta(minutes=15)),
    }
    if times is not None:
        morning = datetime(2026, 11, 10, 3, tzinfo=UTC)
        expected |= {
            ("forecasts", morning, morning.date() + timedelta(days=1), None),
            ("estimated_actuals", morning, morning.date(), morning + timedelta(minutes=15)),
        }
    assert created == expected


def edit_clock_and_collect(system: System, moment: datetime) -> CollectionJob:
    clock = moment.astimezone(ISTANBUL).strftime("%H:%M")
    configure_schedule(
        system.member, system.tenant.pk, system.region.pk, enabled=True, times=[clock], now=moment
    )
    enqueue_due(moment)
    job = CollectionJob.objects.get(
        binding=system.binding, endpoint="forecasts", scheduled_for=moment
    )
    collect(job.pk, HistoricalFixture(moment), calculate, moment)
    job.refresh_from_db()
    return job


def forecast_requests(binding: SiteBinding) -> int:
    return RequestAttempt.objects.filter(
        binding=binding, purpose="forecast", utc_day=NOW.date()
    ).count()


def test_clock_edits_spend_no_more_forecast_requests_than_the_saved_clocks() -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=10)
    approval = CollectionJob.objects.get()
    assert collect(approval.pk, HistoricalFixture(NOW), calculate, NOW)

    edited = [
        edit_clock_and_collect(system, NOW + timedelta(minutes=minute))
        for minute in (10, 20, 30, 40, 50)
    ]

    assert forecast_requests(system.binding) == 2
    assert edited[0].status == "succeeded"
    for job in edited[1:]:
        assert (job.status, job.last_error, job.attempts) in {
            ("stale", "quota_exhausted", 0),
            ("stale", "forecast_superseded", 0),
        }


def dispatch_round(moment: datetime) -> list[int]:
    """One dispatcher round as forecasts.runtime runs it, with the worker collecting each job."""
    enqueue_due(moment)
    published = pending_job_ids(moment)
    for job_id in published:
        collect(job_id, HistoricalFixture(moment), calculate, moment)
    return published


def rounds_from(start: datetime, count: int) -> list[datetime]:
    return [start + timedelta(seconds=30 * step) for step in range(count)]


def assert_rests_for_its_slot(
    system: System, refused: CollectionJob, moments: list[datetime]
) -> None:
    for moment in moments:
        enqueue_due(moment)
        refused.refresh_from_db()
        assert (refused.status, refused.last_error, refused.attempts) == (
            "stale",
            "quota_exhausted",
            0,
        ), moment
        account = ProviderAccount.objects.get(pk=system.account.pk)
        next_day_slot = 0 if moment.date() == refused.scheduled_for.date() else 1
        assert remaining_forecast_requests(account, moment) == next_day_slot, moment
        published = pending_job_ids(moment)
        assert refused.pk not in published, moment
        for job_id in published:
            collect(job_id, HistoricalFixture(moment), calculate, moment)
        assert not RequestAttempt.objects.filter(job=refused).exists(), moment


def forecast_requests_on(binding: SiteBinding, moment: datetime) -> int:
    return RequestAttempt.objects.filter(
        binding=binding, purpose="forecast", utc_day=moment.date()
    ).count()


def assert_next_slot_collects(system: System, slot: datetime) -> None:
    published = dispatch_round(slot)
    job = CollectionJob.objects.get(
        binding=system.binding, endpoint="forecasts", scheduled_for=slot
    )
    assert job.pk in published
    assert (job.status, job.target_date) == (
        "succeeded",
        slot.astimezone(ISTANBUL).date() + timedelta(days=1),
    )
    assert RequestAttempt.objects.filter(job=job, utc_day=slot.date()).count() == 1
    assert forecast_requests_on(system.binding, slot) == 1


def test_a_job_past_the_daily_bound_rests_for_its_slot_after_an_edited_clock() -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=10)
    edit_clock_and_collect(system, NOW + timedelta(minutes=10))

    refused = edit_clock_and_collect(system, NOW + timedelta(minutes=20))

    assert (refused.status, refused.last_error, refused.attempts) == ("stale", "quota_exhausted", 0)
    midnight = next_provider_day(NOW)
    assert_rests_for_its_slot(
        system,
        refused,
        [
            *rounds_from(refused.scheduled_for + timedelta(seconds=30), 6),
            *rounds_from(midnight - timedelta(seconds=60), 5),
        ],
    )
    assert forecast_requests_on(system.binding, midnight) == 0
    assert_next_slot_collects(system, refused.scheduled_for + timedelta(days=1))


def test_a_job_past_the_daily_bound_rests_for_its_slot_under_one_clock() -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=10)
    CollectionSchedule.objects.create(
        region=system.region, times=["02:50"], effective_from=NOW - timedelta(days=1)
    )
    late_slot = datetime(2026, 11, 9, 23, 50, tzinfo=UTC)
    late = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="forecasts",
        scheduled_for=late_slot,
        target_date=late_slot.astimezone(ISTANBUL).date() + timedelta(days=1),
        available_at=late_slot,
    )
    delayed = next_provider_day(late_slot) + timedelta(minutes=5)
    reserve_request(system.binding, delayed, purpose=RequestPurpose.FORECAST, job=late)
    CollectionJob.objects.filter(pk=late.pk).update(status="succeeded")
    slot = late_slot + timedelta(days=1)

    dispatch_round(slot)

    refused = CollectionJob.objects.get(
        binding=system.binding, endpoint="forecasts", scheduled_for=slot
    )
    assert (refused.status, refused.last_error, refused.attempts) == ("stale", "quota_exhausted", 0)
    midnight = next_provider_day(slot)
    assert_rests_for_its_slot(
        system,
        refused,
        [
            *rounds_from(slot + timedelta(seconds=30), 6),
            *rounds_from(midnight - timedelta(seconds=60), 5),
        ],
    )
    assert forecast_requests_on(system.binding, midnight) == 0
    assert_next_slot_collects(system, slot + timedelta(days=1))


SLOT_FINAL = {CollectionJobError.QUOTA_EXHAUSTED, CollectionJobError.SCHEDULE_DISABLED}
SLOT_REVIVABLE = {
    "",
    CollectionJobError.HISTORY_WINDOW_CLOSED,
    CollectionJobError.ATTEMPTS_EXHAUSTED,
    CollectionJobError.FORECAST_SUPERSEDED,
    CollectionJobError.BINDING_NOT_CURRENT,
    CollectionJobError.SLOT_NOT_CONFIGURED,
    CollectionJobError.SCHEDULE_CHANGED,
    CollectionJobError.HISTORY_INCOMPLETE,
    CollectionJobError.HISTORY_ALREADY_ARCHIVED,
    CollectionJobError.PROVIDER_AUTHENTICATION_FAILED,
    CollectionJobError.PROVIDER_ALLOWANCE_EXHAUSTED,
    CollectionJobError.PROVIDER_ACCESS_DENIED,
    CollectionJobError.PROVIDER_SITE_NOT_FOUND,
    CollectionJobError.PROVIDER_THROTTLED,
    CollectionJobError.PROVIDER_SERVER_ERROR,
    CollectionJobError.PROVIDER_UNREACHABLE,
    CollectionJobError.PROVIDER_INVALID_RESPONSE,
    CollectionJobError.PROVIDER_CONFIGURATION_INVALID,
    CollectionJobError.ACCOUNT_UNAVAILABLE,
    CollectionJobError.HISTORY_BUDGET_RESERVED,
    CollectionJobError.PROVIDER_FAILURE,
    CollectionJobError.PROVIDER_UNEXPECTED_FAILURE,
    CollectionJobError.INVALID_SOURCE_KIND,
    CollectionJobError.CALCULATION_FAILURE,
    CollectionJobError.LOCK_CONTENTION,
    CollectionJobError.PUBLICATION_FAILURE,
}


def test_the_scheduler_reopens_a_stale_latest_slot_unless_its_refusal_holds_for_the_slot() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    assert SLOT_FINAL.isdisjoint(SLOT_REVIVABLE)
    assert {"", *CollectionJobError.values} == SLOT_FINAL | SLOT_REVIVABLE

    reopened = set()
    for code in sorted(SLOT_FINAL | SLOT_REVIVABLE):
        CollectionJob.objects.filter(pk=job.pk).update(status="stale", last_error=code)
        enqueue_due(NOW + timedelta(minutes=1))
        job.refresh_from_db()
        if job.status == "pending":
            assert job.last_error == ""
            reopened.add(code)
        else:
            assert (job.status, job.last_error) == ("stale", code)

    assert reopened == SLOT_REVIVABLE


def test_a_retry_after_utc_midnight_leaves_the_next_day_its_collection() -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=10)
    CollectionSchedule.objects.create(
        region=system.region, times=["02:50"], effective_from=NOW - timedelta(days=1)
    )
    slots = [datetime(2026, 11, 9, 23, 50, tzinfo=UTC) + timedelta(days=day) for day in (0, 1)]
    late, next_day = (
        CollectionJob.objects.create(
            binding=system.binding,
            endpoint="forecasts",
            scheduled_for=slot,
            target_date=slot.date() + timedelta(days=2),
            available_at=slot,
        )
        for slot in slots
    )

    reserve_request(system.binding, slots[0], purpose=RequestPurpose.FORECAST, job=late)
    reserve_request(
        system.binding,
        slots[0] + timedelta(minutes=20),
        purpose=RequestPurpose.FORECAST,
        job=late,
    )
    reserve_request(system.binding, slots[1], purpose=RequestPurpose.FORECAST, job=next_day)

    assert RequestAttempt.objects.filter(job=next_day).count() == 1


def test_the_daily_bound_is_per_binding_and_admits_retries_and_the_approval_job() -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=10)
    approval = CollectionJob.objects.get()
    first = edit_clock_and_collect(system, NOW + timedelta(minutes=10))
    assert first.status == "succeeded"
    other = second_binding(system)
    other_job = CollectionJob.objects.get(binding=other)
    second = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="forecasts",
        scheduled_for=NOW + timedelta(minutes=15),
        target_date=NOW.date() + timedelta(days=1),
        available_at=NOW,
    )
    later = NOW + timedelta(minutes=20)

    for job in (first, approval):
        reserve_request(system.binding, later, purpose=RequestPurpose.FORECAST, job=job)
    reserve_request(other, later, purpose=RequestPurpose.FORECAST, job=other_job)
    with pytest.raises(QuotaExceededError):
        reserve_request(system.binding, later, purpose=RequestPurpose.FORECAST, job=second)


def test_concurrent_reservations_of_one_binding_stay_inside_its_daily_bound() -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=10)
    CollectionSchedule.objects.create(
        region=system.region, times=["16:00"], effective_from=NOW - timedelta(days=1)
    )
    jobs = [
        CollectionJob.objects.create(
            binding=system.binding,
            endpoint="forecasts",
            scheduled_for=NOW + timedelta(minutes=minute),
            target_date=NOW.date() + timedelta(days=1),
            available_at=NOW,
        )
        for minute in (1, 2)
    ]
    barrier = Barrier(2)

    def reserve(job: CollectionJob) -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            reserve_request(system.binding, NOW, purpose=RequestPurpose.FORECAST, job=job)
        except QuotaExceededError:
            return False
        finally:
            connection.close()
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = list(pool.map(reserve, jobs))

    assert sorted(admitted) == [False, True]
    assert forecast_requests(system.binding) == 1


def test_a_binding_with_collection_disabled_is_refused_forecast_requests() -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=10)
    CollectionSchedule.objects.create(
        region=system.region, enabled=False, times=["16:00"], effective_from=NOW
    )
    job = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="forecasts",
        scheduled_for=NOW + timedelta(minutes=1),
        target_date=NOW.date() + timedelta(days=1),
        available_at=NOW,
    )

    with pytest.raises(ScheduleDisabledError):
        reserve_request(system.binding, NOW, purpose=RequestPurpose.FORECAST, job=job)

    assert forecast_requests(system.binding) == 0


class DisablingAdapter(FakeAdapter):
    def prepare(self, binding: SiteBinding) -> None:
        CollectionSchedule.objects.filter(region_id=binding.region_id).update(enabled=False)


def test_collection_disabled_after_the_claim_skips_the_job_without_a_request() -> None:
    system = make_system(approved=True)
    CollectionSchedule.objects.create(
        region=system.region, times=["16:00"], effective_from=NOW - timedelta(days=1)
    )
    job = CollectionJob.objects.get()

    assert not collect(job.pk, DisablingAdapter(), calculate, NOW)

    job.refresh_from_db()
    assert (job.status, job.last_error, job.attempts) == ("stale", "schedule_disabled", 0)
    assert not RequestAttempt.objects.filter(job=job).exists()


def test_the_daily_bound_reads_the_quota_of_the_locked_account() -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=10)
    CollectionSchedule.objects.create(
        region=system.region, times=["16:00", "17:00"], effective_from=NOW - timedelta(days=1)
    )
    binding = SiteBinding.objects.select_related("provider_account").get(pk=system.binding.pk)
    tomorrow = NOW + timedelta(days=1)
    first, second = (
        CollectionJob.objects.create(
            binding=binding,
            endpoint="forecasts",
            scheduled_for=tomorrow + timedelta(minutes=minute),
            target_date=tomorrow.date() + timedelta(days=1),
            available_at=tomorrow,
        )
        for minute in (1, 2)
    )
    reserve_request(binding, tomorrow, purpose=RequestPurpose.FORECAST, job=first)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=2)

    with pytest.raises(BindingDayExhaustedError):
        reserve_request(binding, tomorrow, purpose=RequestPurpose.FORECAST, job=second)

    assert RequestAttempt.objects.filter(binding=binding, utc_day=tomorrow.date()).count() == 1
