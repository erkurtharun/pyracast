from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier

import pytest
from django.db import close_old_connections, connections

from pyracast.metering.models import EpiasCollectionJob, EpiasConnection
from pyracast.metering.scheduler import enqueue_connection, enqueue_due_epias_jobs
from pyracast.plants.models import PlantConfiguration
from pyracast.plants.operations import revise_plant
from tests.test_domain import NOW
from tests.test_metering import NEXT_NIGHT, connected

pytestmark = pytest.mark.django_db(transaction=True)


def test_initial_history_covers_ninety_completed_market_days_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, linked = connected(monkeypatch)
    jobs = EpiasCollectionJob.objects.order_by("pk")
    initial_ids = list(jobs.values_list("pk", flat=True))
    assert len(initial_ids) == 90
    assert set(jobs.values_list("target_day", flat=True)) == {
        NOW.date() - timedelta(days=offset) for offset in range(1, 91)
    }
    assert set(jobs.values_list("scheduled_for", flat=True)) == {linked.updated_at}
    assert enqueue_connection(linked, NOW + timedelta(minutes=1), initial=True) == initial_ids
    enqueue_due_epias_jobs(now=NOW + timedelta(minutes=2))
    assert list(jobs.values_list("pk", flat=True)) == initial_ids


def test_configuration_revision_enqueues_its_own_ninety_day_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, linked = connected(monkeypatch)
    prior_configuration_id = plant.current_configuration_id
    revised = revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name=plant.name,
        dc_capacity_mw=140,
        export_limit_mw=100,
    )
    jobs = EpiasCollectionJob.objects.filter(connection=linked)
    assert jobs.filter(configuration_id=prior_configuration_id).count() == 90
    current_jobs = jobs.filter(configuration_id=revised.current_configuration_id)
    assert current_jobs.count() == 90
    assert set(current_jobs.values_list("target_day", flat=True)) == {
        NOW.date() - timedelta(days=offset) for offset in range(1, 91)
    }
    enqueue_due_epias_jobs(now=NOW + timedelta(minutes=1))
    assert current_jobs.count() == 90


def test_existing_seven_day_connection_backfills_once_with_concurrent_schedulers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected(monkeypatch)
    EpiasCollectionJob.objects.filter(target_day__lt=NOW.date() - timedelta(days=7)).delete()
    assert EpiasCollectionJob.objects.count() == 7
    barrier = Barrier(2)
    dispatch_at = NEXT_NIGHT

    def dispatch() -> list[int]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return enqueue_due_epias_jobs(now=dispatch_at)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: dispatch(), range(2)))
    assert EpiasCollectionJob.objects.count() == 97
    assert len(set().union(*map(set, results))) == 97
    assert len(enqueue_due_epias_jobs(now=dispatch_at + timedelta(minutes=1))) == 7
    assert EpiasCollectionJob.objects.count() == 97


def test_initial_settlement_includes_fourth_month_when_ninety_days_require_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plant, linked = connected(monkeypatch)
    EpiasCollectionJob.objects.all().delete()
    initial_at = datetime(2026, 5, 1, 13, tzinfo=UTC)
    EpiasConnection.objects.filter(pk=linked.pk).update(
        updated_at=initial_at, settlement_plant_id=7001
    )
    assert plant.current_configuration_id is not None
    PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(
        created_at=initial_at - timedelta(days=100)
    )
    linked.refresh_from_db()
    identifiers = enqueue_connection(linked, initial_at, initial=True)
    assert len(identifiers) == 94
    assert list(
        EpiasCollectionJob.objects.filter(source_kind="settlement_injection")
        .order_by("target_day")
        .values_list("target_day", "period_end_day")
    ) == [
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 31)),
        (date(2026, 4, 1), date(2026, 4, 30)),
    ]
    assert (
        enqueue_connection(linked, initial_at + timedelta(minutes=1), initial=True) == identifiers
    )
    assert EpiasCollectionJob.objects.count() == 94
