from datetime import date, timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.background.failures import PhaseOutcome
from pyracast.connections.contracts import ProviderData
from pyracast.connections.currency import binding_is_current
from pyracast.forecasts.jobs import (
    claim,
    collect,
    pending_job_ids,
    persist_snapshot,
    publish,
)
from pyracast.forecasts.management.commands import dispatch as dispatch_command
from pyracast.forecasts.models import CollectionJob, ForecastResult, RequestAttempt
from pyracast.forecasts.scheduler import enqueue_due as enqueue_collections
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.scheduler import enqueue_due as enqueue_reminders
from pyracast.plants.activation import reset_calibration
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import CalibrationRun, Plant
from pyracast.plants.operations import configuration_is_current
from pyracast.plants.preparation import prepare_calibration
from pyracast.plants.training import run_calibration
from pyracast.regions.models import Region
from pyracast.trash import purge_expired, restore_region, trash_region
from tests.support.domain import NOW, FakeAdapter
from tests.support.jobs import calculate
from tests.support.notifications import SLOT, configured, saved
from tests.support.plant_operations import make_plant, request_run
from tests.support.trash_boundaries import _race_before_region_lock

pytestmark = pytest.mark.django_db(transaction=True)


def test_trashed_location_blocks_domain_and_restores_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    trash_region(system.member, system.tenant.pk, system.region.pk)
    assert not binding_is_current(system.binding)
    assert not configuration_is_current(configuration)
    assert prepare_calibration(plant.pk) is None
    with pytest.raises(Region.DoesNotExist):
        reset_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk)
    with pytest.raises(Region.DoesNotExist):
        plant_outlook(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            target_date=date(2026, 11, 10),
            resolution_minutes=30,
        )
    restore_region(system.member, system.tenant.pk, system.region.pk)
    assert configuration_is_current(configuration)


def test_region_trash_prevents_inflight_publication_and_new_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    job = CollectionJob.objects.get()
    lease = claim(job.pk, NOW)
    assert lease is not None
    snapshot = persist_snapshot(lease, ProviderData({}, ()), NOW)
    assert snapshot is not None
    trash_region(system.member, system.tenant.pk, system.region.pk)
    assert not publish(lease, snapshot, {}, NOW)
    assert not ForecastResult.objects.exists()
    assert enqueue_collections(NOW + timedelta(days=1)) == []
    assert enqueue_reminders(NOW + timedelta(days=1)) == []
    CollectionJob.objects.filter(pk=job.pk).update(status="retry", available_at=NOW)
    assert pending_job_ids(NOW) == []
    assert not collect(job.pk, FakeAdapter(), calculate, NOW)
    assert not RequestAttempt.objects.filter(purpose="forecast").exists()


def test_purged_queue_identifiers_are_ignored() -> None:
    assert claim(987654321, NOW) is None
    assert not collect(987654321, FakeAdapter(), calculate, NOW)
    assert not run_calibration(987654321, NOW)
    assert not process_notification(987654321, NOW)


def test_dispatch_prioritizes_notifications_and_continues_all_queues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    broker = Mock()
    monkeypatch.setattr(dispatch_command, "make_broker", lambda: broker)
    for name in (
        "make_actor",
        "make_calibration_actor",
        "make_notification_actor",
        "make_outbound_mail_actor",
        "make_epias_actor",
    ):
        monkeypatch.setattr(dispatch_command, name, lambda broker: object())
    monkeypatch.setattr(dispatch_command, "purge_expired", lambda now: calls.append("purge"))
    monkeypatch.setattr(dispatch_command, "sweep_accounts", lambda now: calls.append("sweep"))
    monkeypatch.setattr(dispatch_command, "purge_finished_mail", lambda now: None)
    for name in ("dispatch_calibrations", "dispatch_notifications", "dispatch_epias"):
        monkeypatch.setattr(dispatch_command, name, lambda *actors, name=name: calls.append(name))

    def collection(actor: object) -> PhaseOutcome:
        calls.append("dispatch")
        return PhaseOutcome()

    monkeypatch.setattr(dispatch_command, "dispatch_collections", collection)
    dispatch_command.Command().handle(loop=False, poll_seconds=60.0)
    assert calls == [
        "dispatch_notifications",
        "dispatch",
        "dispatch_calibrations",
        "purge",
        "sweep",
        "dispatch_epias",
    ]
    broker.close.assert_called_once_with()


def test_collection_candidate_purged_before_region_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _ = make_plant(monkeypatch)

    def purge() -> None:
        trash_region(system.member, system.tenant.pk, system.region.pk)
        purge_expired(NOW + timedelta(days=31))

    assert (
        _race_before_region_lock(
            monkeypatch, "first", lambda: enqueue_collections(NOW + timedelta(days=1)), purge
        )
        == []
    )


def test_reminder_candidate_purged_before_region_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system, _ = configured()
    saved(system)

    def purge() -> None:
        trash_region(system.member, system.tenant.pk, system.region.pk)
        purge_expired(NOW + timedelta(days=31))

    assert (
        _race_before_region_lock(
            monkeypatch, "get", lambda: enqueue_reminders(SLOT), purge, lock_model=Tenant
        )
        == []
    )


def test_calibration_identity_purged_before_region_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)

    def purge() -> None:
        trash_region(system.member, system.tenant.pk, system.region.pk)
        purge_expired(NOW + timedelta(days=31))

    assert (
        _race_before_region_lock(
            monkeypatch, "get", lambda: run_calibration(run.pk, NOW), purge, lock_model=Tenant
        )
        is False
    )
    assert not Plant.objects.filter(pk=plant.pk).exists()
    assert not CalibrationRun.objects.filter(pk=run.pk).exists()
