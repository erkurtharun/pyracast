import json
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
from threading import Barrier

import pytest
from django.db import close_old_connections, connections

from pyracast.collection_window import nightly_window
from pyracast.epias import TIMEZONE, EpiasDay, EpiasPlant
from pyracast.metering.jobs import run_epias_job
from pyracast.metering.models import EpiasCollectionJob, EpiasSnapshot
from pyracast.metering.scheduler import enqueue_due_epias_jobs
from pyracast.regions.models import Region
from tests.support.domain import NOW
from tests.support.metering import NEXT_NIGHT, connected, day_data
from tests.support.metering_settlement import SETTLEMENT, link_settlement, month_data

pytestmark = pytest.mark.django_db(transaction=True)

REPAIR_NIGHTS = 3
REPAIRS_PER_NIGHT = 31
# At NEXT_NIGHT the market day is 2026-11-10: the rolling window re-collects 11-03 to 11-09.
NEWEST_REPAIRABLE = date(2026, 11, 2)
OLDEST_REPAIRABLE = date(2026, 8, 12)
# Settlement months 1 to 3 back are collected each night; months 4 to 12 back are repaired.
NEWEST_REPAIRABLE_MONTH = date(2026, 7, 1)
OLDEST_REPAIRABLE_MONTH = date(2025, 11, 1)


def night(offset: int) -> datetime:
    return NEXT_NIGHT + timedelta(days=offset)


def window(moment: datetime) -> tuple[datetime, datetime]:
    bounds = nightly_window(moment.astimezone(TIMEZONE).date(), "Europe/Istanbul")
    assert bounds is not None
    return bounds


def fail(days: Iterable[date]) -> None:
    EpiasCollectionJob.objects.filter(target_date__in=list(days)).update(
        status="failed", attempts=3, last_error="attempts_exhausted"
    )


def repairs(moment: datetime) -> list[date]:
    return list(
        EpiasCollectionJob.objects.filter(
            scheduled_for=window(moment)[0], target_date__lte=NEWEST_REPAIRABLE
        )
        .order_by("-target_date")
        .values_list("target_date", flat=True)
    )


def test_a_failed_day_inside_the_horizon_is_collected_on_the_next_night(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected(monkeypatch)
    day = date(2026, 10, 10)
    first = EpiasCollectionJob.objects.get(target_date=day)
    fail([day])

    identifiers = enqueue_due_epias_jobs(now=NEXT_NIGHT)

    repair = EpiasCollectionJob.objects.exclude(pk=first.pk).get(target_date=day)
    assert repair.pk in identifiers
    assert len(identifiers) == 8
    assert (repair.status, repair.attempts) == ("pending", 0)
    assert (repair.scheduled_for, repair.retry_until) == window(NEXT_NIGHT)
    assert (repair.connection_revision, repair.configuration_id, repair.provider_plant) == (
        first.connection_revision,
        first.configuration_id,
        first.provider_plant,
    )
    assert enqueue_due_epias_jobs(now=NEXT_NIGHT + timedelta(minutes=30)) == identifiers


def test_repair_stays_inside_the_horizon_and_skips_collected_or_rolling_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected(monkeypatch)
    partial_day = date(2026, 10, 20)
    partial_job = EpiasCollectionJob.objects.get(target_date=partial_day)

    def partial(plant: EpiasPlant, day: date) -> EpiasDay:
        complete = day_data(plant, day)
        return replace(complete, readings=complete.readings[:12])

    run_epias_job(partial_job.pk, fetch=partial, now=NEXT_NIGHT - timedelta(hours=10))
    fail([date(2026, 8, 11), partial_day, date(2026, 11, 5)])

    identifiers = enqueue_due_epias_jobs(now=NEXT_NIGHT)

    assert len(identifiers) == 7
    assert repairs(NEXT_NIGHT) == []
    assert EpiasCollectionJob.objects.filter(target_date=date(2026, 8, 11)).count() == 1
    assert EpiasCollectionJob.objects.filter(target_date=partial_day).count() == 1


def test_a_day_that_keeps_failing_is_repaired_on_three_nights_and_then_left_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected(monkeypatch)
    day = OLDEST_REPAIRABLE + timedelta(days=REPAIR_NIGHTS + 1)
    fail([day])
    for offset in range(REPAIR_NIGHTS):
        enqueue_due_epias_jobs(now=night(offset))
        assert repairs(night(offset)) == [day]
        fail([day])

    enqueue_due_epias_jobs(now=night(REPAIR_NIGHTS))

    assert repairs(night(REPAIR_NIGHTS)) == []
    jobs = EpiasCollectionJob.objects.filter(target_date=day)
    assert jobs.count() == 1 + REPAIR_NIGHTS
    assert set(jobs.values_list("status", flat=True)) == {"failed"}


def test_a_day_that_failed_in_the_rolling_window_is_still_repaired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected(monkeypatch)
    day = NEWEST_REPAIRABLE
    fail([day])
    initial = EpiasCollectionJob.objects.get(target_date=day)
    for nights_before in range(1, REPAIR_NIGHTS + 2):
        slot, end = window(night(-nights_before))
        for target in (day, day + timedelta(days=1)):
            EpiasCollectionJob.objects.create(
                connection=initial.connection,
                plant=initial.plant,
                connection_revision=initial.connection_revision,
                configuration=initial.configuration,
                provider_plant=initial.provider_plant,
                target_date=target,
                scheduled_for=slot,
                available_at=slot,
                retry_until=end,
                status="failed",
                attempts=3,
                last_error="epias_provider_failure",
            )

    enqueue_due_epias_jobs(now=NEXT_NIGHT)

    assert repairs(NEXT_NIGHT) == [day]


def test_repair_nights_that_sent_no_request_are_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected(monkeypatch)
    day = OLDEST_REPAIRABLE + timedelta(days=REPAIR_NIGHTS + 1)
    fail([day])
    for offset in range(REPAIR_NIGHTS):
        enqueue_due_epias_jobs(now=night(offset))
        assert repairs(night(offset)) == [day]
        EpiasCollectionJob.objects.filter(
            target_date=day, scheduled_for=window(night(offset))[0]
        ).update(status="failed", attempts=0, last_error="epias_credentials_missing")

    enqueue_due_epias_jobs(now=night(REPAIR_NIGHTS))

    assert repairs(night(REPAIR_NIGHTS)) == [day]


def test_repairs_of_one_connection_are_capped_per_night_newest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected(monkeypatch)
    days = [NEWEST_REPAIRABLE - timedelta(days=offset) for offset in range(40)]
    fail(days)

    enqueue_due_epias_jobs(now=NEXT_NIGHT)
    enqueue_due_epias_jobs(now=NEXT_NIGHT + timedelta(hours=1))
    enqueue_due_epias_jobs(now=night(1))

    assert repairs(NEXT_NIGHT) == days[:REPAIRS_PER_NIGHT]
    assert repairs(night(1)) == days[REPAIRS_PER_NIGHT:]


def test_concurrent_schedulers_repair_each_slot_once_inside_the_nightly_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected(monkeypatch)
    days = [NEWEST_REPAIRABLE - timedelta(days=offset) for offset in range(40)]
    fail(days)
    barrier = Barrier(2)

    def enqueue() -> list[int]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return enqueue_due_epias_jobs(now=NEXT_NIGHT)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: enqueue(), range(2)))

    assert results[0] == results[1]
    assert repairs(NEXT_NIGHT) == days[:REPAIRS_PER_NIGHT]
    assert EpiasCollectionJob.objects.count() == 90 + 7 + REPAIRS_PER_NIGHT


def settlement(month: date) -> EpiasCollectionJob:
    return EpiasCollectionJob.objects.get(source_kind="settlement_injection", target_date=month)


def fail_months(months: Iterable[date]) -> None:
    EpiasCollectionJob.objects.filter(
        source_kind="settlement_injection", target_date__in=list(months)
    ).update(status="failed", attempts=3, last_error="attempts_exhausted")


def settlement_repairs(moment: datetime) -> list[tuple[date, date | None]]:
    return list(
        EpiasCollectionJob.objects.filter(
            source_kind="settlement_injection",
            scheduled_for=window(moment)[0],
            target_date__lte=NEWEST_REPAIRABLE_MONTH,
        )
        .order_by("-target_date")
        .values_list("target_date", "period_end_day")
    )


def test_settlement_months_four_to_twelve_back_without_a_snapshot_are_repaired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link_settlement(monkeypatch)
    late_days = settlement(date(2026, 4, 1))
    assert run_epias_job(
        late_days.pk,
        fetch=lambda *_: month_data(SETTLEMENT, date(2026, 4, 1), date(2026, 4, 30)),
        now=NOW,
    )
    EpiasSnapshot.objects.filter(job=late_days, target_date__lt=date(2026, 4, 15)).delete()
    unpublished = settlement(date(2026, 3, 1))
    empty = month_data(SETTLEMENT, unpublished.target_date, date(2026, 3, 31))
    assert not run_epias_job(
        unpublished.pk,
        fetch=lambda *_: replace(empty, readings=(), raw_payload=json.dumps({"items": []})),
        now=NOW,
    )
    assert settlement(date(2026, 3, 1)).status == "awaiting_publication"
    fail_months(
        [date(2026, 8, 1), NEWEST_REPAIRABLE_MONTH, date(2026, 4, 1), OLDEST_REPAIRABLE_MONTH]
    )

    enqueue_due_epias_jobs(now=NEXT_NIGHT)

    assert settlement_repairs(NEXT_NIGHT) == [
        (NEWEST_REPAIRABLE_MONTH, date(2026, 7, 31)),
        (OLDEST_REPAIRABLE_MONTH, date(2025, 11, 30)),
    ]
    assert EpiasCollectionJob.objects.filter(target_date=date(2026, 8, 1)).count() == 1
    assert not EpiasCollectionJob.objects.filter(target_date__lt=OLDEST_REPAIRABLE_MONTH).exclude(
        source_kind="realtime_generation"
    )
    repair = EpiasCollectionJob.objects.get(
        target_date=OLDEST_REPAIRABLE_MONTH, scheduled_for=window(NEXT_NIGHT)[0]
    )
    assert (repair.status, repair.attempts, repair.retry_until) == (
        "pending",
        0,
        window(NEXT_NIGHT)[1],
    )


def test_a_settlement_month_that_keeps_failing_is_repaired_on_three_nights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link_settlement(monkeypatch)
    month = OLDEST_REPAIRABLE_MONTH
    fail_months([month])
    for offset in range(REPAIR_NIGHTS):
        enqueue_due_epias_jobs(now=night(offset))
        assert settlement_repairs(night(offset)) == [(month, date(2025, 11, 30))]
        fail_months([month])

    enqueue_due_epias_jobs(now=night(REPAIR_NIGHTS))

    assert settlement_repairs(night(REPAIR_NIGHTS)) == []
    jobs = EpiasCollectionJob.objects.filter(source_kind="settlement_injection", target_date=month)
    assert jobs.count() == 1 + REPAIR_NIGHTS
    assert set(jobs.values_list("status", flat=True)) == {"failed"}


def test_a_suspended_plant_is_not_repaired(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _, _ = connected(monkeypatch)
    fail([date(2026, 10, 10)])
    Region.objects.filter(pk=system.region.pk).update(status="suspended")

    assert enqueue_due_epias_jobs(now=NEXT_NIGHT) == []
    assert repairs(NEXT_NIGHT) == []
