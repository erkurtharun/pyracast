import json
from calendar import monthrange
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from django.db import connection

from pyracast.epias import (
    BASE_URL,
    EpiasClient,
    EpiasDay,
    EpiasError,
    EpiasPlant,
)
from pyracast.metering.jobs import pending_epias_ids, run_epias_job
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.metering.scheduler import enqueue_due_epias_jobs
from pyracast.plants.models import Plant, ProductionObservation
from pyracast.regions.models import Region
from tests.support.domain import NOW
from tests.support.metering import NEXT_NIGHT, connected
from tests.support.metering_settlement import (
    SETTLEMENT,
    SettlementClient,
    link_settlement,
    month_data,
)

pytestmark = pytest.mark.django_db(transaction=True)


def test_month_collection_splits_immutable_daily_sources_without_manual_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked = link_settlement(monkeypatch)
    assert linked.settlement_plant_id == 7001
    jobs = EpiasCollectionJob.objects.filter(source_kind="settlement_injection")
    assert jobs.count() == 12
    assert EpiasCollectionJob.objects.filter(source_kind="realtime_generation").count() == 90
    assert jobs.earliest("target_date").target_date == date(2025, 11, 1)
    assert list(jobs.order_by("target_date").values_list("target_date", "period_end_day")) == [
        (date(year, month, 1), date(year, month, monthrange(year, month)[1]))
        for year, month in [(2025, 11), (2025, 12), *((2026, month) for month in range(1, 11))]
    ]
    job = jobs.get(target_date=date(2026, 9, 1))
    prepared: list[int] = []

    def prepare(plant_id: int, now: datetime | None = None) -> None:
        assert connection.in_atomic_block
        assert EpiasSnapshot.objects.filter(job=job).count() == 30
        prepared.append(plant_id)

    monkeypatch.setattr("pyracast.plants.preparation.prepare_calibration", prepare)
    monkeypatch.setattr("pyracast.metering.jobs.get_epias_client", lambda: SettlementClient())
    assert run_epias_job(job.pk, now=NOW)
    assert not run_epias_job(job.pk, now=NOW)
    snapshots = EpiasSnapshot.objects.filter(job=job).order_by("target_date")
    assert snapshots.count() == 30
    assert prepared == [linked.plant_id]
    assert not ProductionObservation.objects.exists()
    for snapshot in snapshots:
        assert snapshot.source_kind == "settlement_injection"
        assert snapshot.provider_plant["id"] == 7001
        assert snapshot.provider_plant["eic"] == linked.eic
        assert snapshot.normalization_policy == "hour-start-istanbul-v1"
        assert snapshot.endpoint == f"{BASE_URL}/injection-quantity"
        assert snapshot.complete
        assert len(snapshot.readings) == len(snapshot.raw_payload["items"]) == 24
        assert snapshot.raw_payload["items"][10]["hour"] == 10
        assert snapshot.readings[10]["period_start"].endswith("T10:00:00+03:00")
        assert snapshot.readings[10]["period_end"].endswith("T11:00:00+03:00")
        assert {r["total_mwh"] for r in snapshot.readings} == {"1.5"}


@pytest.mark.parametrize("empty", [True, False])
def test_unpublished_and_partial_settlement_remain_missing_and_reconcile_in_next_slot(
    monkeypatch: pytest.MonkeyPatch,
    empty: bool,
) -> None:
    link_settlement(monkeypatch)
    job = EpiasCollectionJob.objects.get(
        source_kind="settlement_injection", target_date=date(2026, 9, 1)
    )
    full = month_data(SETTLEMENT, job.target_date, date(2026, 9, 30))
    assert run_epias_job(job.pk, fetch=lambda *_: full, now=NOW)
    original = EpiasSnapshot.objects.get(job=job, target_date=date(2026, 9, 1))
    identifiers = enqueue_due_epias_jobs(now=NEXT_NIGHT)
    assert len(identifiers) == 9
    revised = EpiasCollectionJob.objects.get(
        pk__in=identifiers, source_kind="settlement_injection", target_date=job.target_date
    )
    rows = (
        ()
        if empty
        else (replace(full.readings[0], total=Decimal(0)), replace(full.readings[1], total=None))
    )
    raw = {
        "items": []
        if empty
        else [
            {"date": full.readings[0].timestamp.isoformat(), "hour": 0, "total": 0},
            {"date": full.readings[1].timestamp.isoformat(), "hour": 1, "total": None},
        ]
    }
    partial = replace(full, readings=rows, raw_payload=json.dumps(raw))
    assert not run_epias_job(revised.pk, fetch=lambda *_: partial, now=NEXT_NIGHT)
    revised.refresh_from_db()
    assert revised.status == "awaiting_publication"
    assert revised.last_error == ("unpublished_data" if empty else "partial_data")
    assert revised.attempts == 1
    assert revised.pk not in pending_epias_ids(NOW + timedelta(days=2))
    assert not run_epias_job(revised.pk, fetch=lambda *_: full, now=NOW + timedelta(days=2))
    latest = EpiasSnapshot.objects.get(job=revised, target_date=original.target_date)
    assert not latest.complete
    assert [r["total_mwh"] for r in latest.readings] == ([] if empty else ["0", None])
    original.refresh_from_db()
    assert original.complete
    assert len(original.readings) == 24
    assert latest.content_sha256 != original.content_sha256
    next_ids = enqueue_due_epias_jobs(now=NEXT_NIGHT + timedelta(days=1))
    assert len(next_ids) == 9
    monday_ids = enqueue_due_epias_jobs(now=NEXT_NIGHT + timedelta(days=6))
    assert len(monday_ids) == 10
    assert enqueue_due_epias_jobs(now=NEXT_NIGHT + timedelta(days=6)) == monday_ids


@pytest.mark.parametrize("outcome", ["resolved", "missing", "failure", "revision_changed"])
def test_existing_connections_resolve_eic_outside_locks_with_daily_bound_and_revision_check(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    _, _, linked = connected(monkeypatch)
    EpiasConnection.objects.filter(pk=linked.pk).update(settlement_checked_at=None)
    calls: list[str] = []

    class Resolver(SettlementClient):
        def list_settlement_plants(self) -> tuple[EpiasPlant, ...]:
            assert not connection.in_atomic_block
            calls.append("list")
            if outcome == "failure":
                raise EpiasError("request_failed")
            if outcome == "revision_changed":
                EpiasConnection.objects.filter(pk=linked.pk).update(revision=2, eic="changed-eic")
            return () if outcome == "missing" else (SETTLEMENT,)

    monkeypatch.setattr("pyracast.metering.scheduler.get_epias_client", lambda: Resolver())
    identifiers = enqueue_due_epias_jobs(now=NEXT_NIGHT + timedelta(hours=1))
    linked.refresh_from_db()
    assert linked.settlement_plant_id == (7001 if outcome == "resolved" else None)
    expected_jobs = 97 if outcome == "revision_changed" else 19 if outcome == "resolved" else 7
    assert len(identifiers) == expected_jobs
    if outcome == "resolved":
        settlement_jobs = EpiasCollectionJob.objects.filter(source_kind="settlement_injection")
        assert settlement_jobs.count() == 12
        assert settlement_jobs.earliest("target_date").target_date == date(2025, 11, 1)
        assert settlement_jobs.latest("target_date").period_end_day == date(2026, 10, 31)
        assert EpiasCollectionJob.objects.filter(source_kind="realtime_generation").count() == 97
    if outcome != "revision_changed":
        assert enqueue_due_epias_jobs(now=NEXT_NIGHT + timedelta(hours=2)) == identifiers
        assert calls == (["list", "list"] if outcome == "failure" else ["list"])
    assert (linked.settlement_checked_at is None) == (outcome == "revision_changed")


@pytest.mark.parametrize("changed", ["eic", "source_id", "source_kind"])
def test_changed_pinned_identity_cannot_publish(
    monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    linked = link_settlement(monkeypatch)
    job = EpiasCollectionJob.objects.filter(source_kind="settlement_injection").first()
    assert job is not None
    if changed == "source_kind":
        EpiasCollectionJob.objects.filter(
            connection=linked,
            source_kind="realtime_generation",
            target_date=job.target_date,
            scheduled_for=job.scheduled_for,
        ).delete()

    def fetch(plant: EpiasPlant, day: date) -> EpiasDay:
        if changed == "eic":
            EpiasConnection.objects.filter(pk=linked.pk).update(eic="different")
        elif changed == "source_id":
            EpiasConnection.objects.filter(pk=linked.pk).update(settlement_plant_id=2)
        else:
            EpiasCollectionJob.objects.filter(pk=job.pk).update(source_kind="realtime_generation")
        assert job.period_end_day is not None
        return month_data(plant, day, job.period_end_day)

    assert not run_epias_job(job.pk, fetch=fetch, now=NOW)
    job.refresh_from_db()
    assert job.status == "superseded"
    assert not EpiasSnapshot.objects.exists()


def test_settlement_publication_persists_while_calibration_preparation_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked = link_settlement(monkeypatch)
    job = EpiasCollectionJob.objects.filter(source_kind="settlement_injection").first()
    assert job is not None
    assert job.period_end_day is not None
    data = month_data(SETTLEMENT, job.target_date, job.period_end_day)

    def fail(plant_id: int, now: datetime | None = None) -> None:
        assert EpiasSnapshot.objects.filter(job=job).exists()
        raise RuntimeError("Preparation interrupted")

    monkeypatch.setattr("pyracast.plants.preparation.prepare_calibration", fail)
    assert run_epias_job(job.pk, fetch=lambda *_: data, now=NOW)
    assert EpiasSnapshot.objects.filter(job=job).count() == 31
    job.refresh_from_db()
    assert (job.status, job.attempts) == ("succeeded", 1)
    plant = Plant.objects.get(pk=linked.plant_id)
    assert plant.calibration_preparation_due_at is not None


@pytest.mark.parametrize("revocation", ["configuration", "approval"])
def test_settlement_discovery_stops_before_http_when_configuration_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
) -> None:
    system, plant, linked = connected(monkeypatch)
    EpiasConnection.objects.filter(pk=linked.pk).update(settlement_checked_at=None)
    if revocation == "configuration":
        Plant.objects.filter(pk=plant.pk).update(current_configuration=None)
    else:
        Region.objects.filter(pk=system.region.pk).update(status="suspended")

    def forbidden() -> EpiasClient:
        pytest.fail("Unavailable configurations must not access provider credentials.")

    monkeypatch.setattr("pyracast.metering.scheduler.get_epias_client", forbidden)
    assert enqueue_due_epias_jobs(now=NOW + timedelta(days=1)) == []
    linked.refresh_from_db()
    assert linked.settlement_checked_at is None
