import itertools
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from django.db import close_old_connections, connection

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.budget import remaining_forecast_requests
from pyracast.forecasts.jobs import collect
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    RequestAttempt,
    RequestPurpose,
)
from pyracast.forecasts.quota import QuotaExceededError, reserve_request
from pyracast.forecasts.schedule import configure_schedule
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.regions.models import Region
from tests.support.budget_reference import reference_forecast_requests
from tests.support.collection_schedule import HistoricalFixture
from tests.support.domain import NOW, System, make_system
from tests.support.jobs import calculate

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
JOB_STATES = (
    ("pending", False),
    ("retry", True),
    ("failed", False),
    ("succeeded", True),
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


def test_the_forecast_budget_equals_the_budget_before_one_timing_function() -> None:
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
    for schedule, quota, (status, attempted), moment in itertools.product(
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
        CollectionJob.objects.filter(pk=planned.pk).update(status=status)
        RequestAttempt.objects.filter(job=planned).delete()
        if attempted:
            RequestAttempt.objects.create(
                provider_account=system.account,
                binding=system.binding,
                job=planned,
                utc_day=moment.date(),
                created_at=moment,
                purpose="forecast",
            )
        account = ProviderAccount.objects.get(pk=system.account.pk)
        assert remaining_forecast_requests(account, moment) == reference_forecast_requests(
            account, moment
        ), (schedule, quota, status, attempted, moment)
        compared += 1
    assert compared == 240


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
            ("retry", "quota_exhausted", 0),
            ("stale", "forecast_superseded", 0),
        }


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
