from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest
from django.db import OperationalError, close_old_connections, connection, connections

from pyracast.calculation import calculate
from pyracast.forecasts.jobs import Lease, claim, collect, publish
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.plants import forecast_archive
from pyracast.plants.models import ForecastIssuance, Plant
from pyracast.plants.operations import revise_plant
from tests.support.domain import NOW, FakeAdapter, System
from tests.support.plant_operations import make_plant, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def pending_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[System, Plant, ForecastSnapshot, Lease]:
    system, plant = make_plant(monkeypatch)
    snapshot = snapshot_for(system, (NOW + timedelta(days=1)).date(), collected_at=NOW)
    lease = claim(snapshot.job_id, NOW)
    assert lease is not None
    return system, plant, snapshot, lease


def test_collection_publishes_electricity_without_an_archive_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot = snapshot_for(system, (NOW + timedelta(days=1)).date(), collected_at=NOW)
    actual_prepare = forecast_archive.prepare_forecast_issuances

    def prepare(source: ForecastSnapshot) -> tuple[forecast_archive.PreparedForecastIssuance, ...]:
        assert not connection.in_atomic_block
        return actual_prepare(source)

    monkeypatch.setattr(forecast_archive, "prepare_forecast_issuances", prepare)
    assert collect(snapshot.job_id, FakeAdapter(), calculate, NOW)
    issuance = ForecastIssuance.objects.get(plant=plant)
    snapshot.refresh_from_db()
    assert issuance.snapshot_id == snapshot.pk
    assert issuance.published_at == snapshot.available_at
    assert issuance.published_at == ForecastResult.objects.get(snapshot=snapshot).published_at
    assert issuance.payloads["30"]["energy_mwh"] == issuance.payloads["60"]["energy_mwh"]
    assert len(issuance.native_intervals) == 48
    assert not RequestAttempt.objects.filter(purpose="forecast").exists()
    original = issuance.payloads
    assert not collect(snapshot.job_id, FakeAdapter(), calculate, NOW)
    issuance.refresh_from_db()
    assert issuance.payloads == original
    assert ForecastIssuance.objects.count() == 1


def test_archive_and_regional_publication_roll_back_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plant, snapshot, lease = pending_publication(monkeypatch)
    actual_save = forecast_archive.save_forecast_issuances

    def fail_after_save(
        prepared: tuple[forecast_archive.PreparedForecastIssuance, ...],
        source: ForecastSnapshot,
        published_at: datetime,
        *,
        plant: Plant | None,
    ) -> None:
        actual_save(prepared, source, published_at, plant=plant)
        assert ForecastIssuance.objects.exists()
        raise OperationalError("Publication interrupted")

    monkeypatch.setattr(forecast_archive, "save_forecast_issuances", fail_after_save)
    with pytest.raises(OperationalError, match="Publication interrupted"):
        publish(lease, snapshot, {}, NOW)
    snapshot.refresh_from_db()
    assert snapshot.available_at is None
    assert not ForecastResult.objects.filter(snapshot=snapshot).exists()
    assert not ForecastIssuance.objects.filter(plant=plant).exists()
    assert CollectionJob.objects.get(pk=lease.job_id).lease_token == lease.token
    assert ForecastSnapshot.objects.filter(pk=snapshot.pk).exists()
    monkeypatch.setattr(forecast_archive, "save_forecast_issuances", actual_save)
    assert publish(lease, snapshot, {}, NOW)
    assert ForecastIssuance.objects.filter(plant=plant).count() == 1


def test_archive_ineligible_mixed_intervals_do_not_block_regional_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = snapshot_for(system, (NOW + timedelta(days=1)).date(), collected_at=NOW)
    source.normalized_intervals[0]["source_kinds"] = ["forecast", "estimated_actuals"]
    source.save(update_fields=("normalized_intervals",))
    assert collect(source.job_id, FakeAdapter(), calculate, NOW)
    source.refresh_from_db()
    assert source.available_at is not None
    assert ForecastResult.objects.filter(snapshot=source).exists()
    assert not ForecastIssuance.objects.filter(plant=plant).exists()
    assert CollectionJob.objects.get(pk=source.job_id).status == "succeeded"


def test_two_workers_cannot_publish_two_electrical_vintages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plant, snapshot, lease = pending_publication(monkeypatch)
    barrier = Barrier(2)

    def attempt() -> bool:
        close_old_connections()
        try:
            source = ForecastSnapshot.objects.get(pk=snapshot.pk)
            prepared = forecast_archive.prepare_forecast_issuances(source)
            barrier.wait(timeout=10)
            return publish(lease, source, {}, NOW, prepared_issuances=prepared)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sorted(outcomes) == [False, True]
    assert ForecastIssuance.objects.filter(plant=plant).count() == 1
    assert ForecastResult.objects.filter(snapshot=snapshot).count() == 1


def test_configuration_changed_during_preparation_cannot_receive_the_old_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, snapshot, lease = pending_publication(monkeypatch)
    prepared = forecast_archive.prepare_forecast_issuances(snapshot)
    assert prepared
    revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name="Reconfigured",
        dc_capacity_mw=150,
        export_limit_mw=100,
    )
    assert publish(lease, snapshot, {}, NOW, prepared_issuances=prepared)
    assert ForecastResult.objects.filter(snapshot=snapshot).exists()
    assert not ForecastIssuance.objects.filter(plant=plant).exists()


def test_expired_worker_leaves_the_acquisition_unpublished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plant, snapshot, lease = pending_publication(monkeypatch)
    prepared = forecast_archive.prepare_forecast_issuances(snapshot)
    assert not publish(lease, snapshot, {}, NOW + timedelta(minutes=6), prepared_issuances=prepared)
    snapshot.refresh_from_db()
    assert snapshot.available_at is None
    assert not ForecastResult.objects.filter(snapshot=snapshot).exists()
    assert not ForecastIssuance.objects.filter(plant=plant).exists()


def test_electrical_failure_keeps_the_acquisition_for_a_request_free_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot = snapshot_for(system, (NOW + timedelta(days=1)).date(), collected_at=NOW)
    actual_prepare = forecast_archive.prepare_forecast_issuances

    def fail(source: ForecastSnapshot) -> tuple[forecast_archive.PreparedForecastIssuance, ...]:
        raise ValueError("Electrical projection unavailable")

    monkeypatch.setattr(forecast_archive, "prepare_forecast_issuances", fail)
    assert not collect(snapshot.job_id, FakeAdapter(), calculate, NOW)
    job = CollectionJob.objects.get(pk=snapshot.job_id)
    assert (job.status, job.last_error) == ("retry", "calculation_failure")
    assert ForecastSnapshot.objects.filter(pk=snapshot.pk).exists()
    assert not ForecastResult.objects.filter(snapshot=snapshot).exists()
    monkeypatch.setattr(forecast_archive, "prepare_forecast_issuances", actual_prepare)
    assert collect(job.pk, FakeAdapter(), calculate, job.available_at)
    assert ForecastIssuance.objects.filter(plant=plant).count() == 1
    assert not RequestAttempt.objects.filter(purpose="forecast").exists()
