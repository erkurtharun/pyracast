from types import SimpleNamespace
from typing import Any

import psycopg.errors
import pytest
from django.db import OperationalError
from django.db.models import Model
from django.utils import timezone

from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import OutboundMail, Tenant
from pyracast.background.claims import claim_unless_contended
from pyracast.connections import operations
from pyracast.connections.models import ProviderAccount
from pyracast.forecasts import runtime as collection_runtime
from pyracast.forecasts.jobs.ready import pending_job_ids
from pyracast.forecasts.models import CollectionJob, ForecastSnapshot, RequestAttempt
from pyracast.forecasts.revalidation import claim_due_revalidations, deliver_revalidation
from pyracast.metering.jobs import pending_epias_ids
from pyracast.metering.models import EpiasCollectionJob
from pyracast.notifications import outbox
from pyracast.notifications import runtime as notification_runtime
from pyracast.notifications.jobs import pending_notification_ids
from pyracast.notifications.models import NotificationDelivery
from pyracast.orchestration.kinds import (
    CALIBRATION,
    COLLECTION,
    METERING,
    OUTBOUND_MAIL,
    REMINDER,
    REVALIDATION,
)
from pyracast.plants import training
from pyracast.plants.models import CalibrationRun
from pyracast.plants.training import pending_calibration_ids
from pyracast.regions.models import Region
from tests.support.concurrency import row_locked_elsewhere, short_lock_timeout
from tests.support.domain import NOW, FakeAdapter, make_system, provider_key
from tests.support.metering import connected
from tests.support.notifications import SLOT, ready
from tests.support.plant_operations import make_plant, request_run

pytestmark = pytest.mark.django_db(transaction=True)

LEASED = ("status", "attempts", "lease_token", "lease_expires_at", "available_at")


def state(model: type[Model], pk: int, fields: tuple[str, ...] = LEASED) -> tuple[Any, ...]:
    return tuple(model._default_manager.values_list(*fields).get(pk=pk))


def contended_lines(logs: pytest.LogCaptureFixture, kind: str, job_id: int) -> list[str]:
    return [
        record.getMessage()
        for record in logs.records
        if record.getMessage().startswith(f"job_claim_contended kind={kind} job_id={job_id} ")
    ]


def database_error(cause: Exception | None) -> OperationalError:
    error = OperationalError("database error")
    error.__cause__ = cause
    return error


def test_a_collection_delivery_behind_a_held_region_lock_ends_and_collects_once_released(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()
    monkeypatch.setattr(collection_runtime, "datetime", SimpleNamespace(now=lambda _: NOW))
    monkeypatch.setattr(collection_runtime, "adapter_for", lambda binding: FakeAdapter())
    before = state(CollectionJob, job.pk)

    with row_locked_elsewhere(Region, system.region.pk), short_lock_timeout():
        COLLECTION.deliver(job.pk)

    assert state(CollectionJob, job.pk) == before
    assert not RequestAttempt.objects.filter(job=job).exists()
    assert len(contended_lines(pyracast_logs, COLLECTION.actor, job.pk)) == 1
    assert job.pk in pending_job_ids(NOW)

    COLLECTION.deliver(job.pk)

    assert CollectionJob.objects.values_list("status", flat=True).get(pk=job.pk) == "succeeded"
    assert ForecastSnapshot.objects.filter(job=job).exists()


def test_a_calibration_delivery_behind_a_held_workspace_lock_ends_without_claiming(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    before = state(CalibrationRun, run.pk, LEASED[:4])

    with row_locked_elsewhere(Tenant, system.tenant.pk), short_lock_timeout():
        CALIBRATION.deliver(run.pk)

    assert state(CalibrationRun, run.pk, LEASED[:4]) == before
    assert len(contended_lines(pyracast_logs, CALIBRATION.actor, run.pk)) == 1
    assert pending_calibration_ids(NOW) == [run.pk]


def test_a_reminder_delivery_behind_a_held_workspace_lock_ends_without_claiming(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    delivery = ready()
    tenant_id = delivery.schedule.region.tenant_id
    monkeypatch.setattr(notification_runtime, "datetime", SimpleNamespace(now=lambda _: SLOT))
    before = state(NotificationDelivery, delivery.pk)

    with row_locked_elsewhere(Tenant, tenant_id), short_lock_timeout():
        REMINDER.deliver(delivery.pk)

    assert state(NotificationDelivery, delivery.pk) == before
    assert len(contended_lines(pyracast_logs, REMINDER.actor, delivery.pk)) == 1
    assert pending_notification_ids(SLOT) == [delivery.pk]


def test_an_outbound_mail_delivery_behind_a_held_mail_lock_ends_without_claiming(
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    system = make_system()
    invite_workspace_member(system.member, system.tenant.pk, "guest@example.com")
    mail = OutboundMail.objects.get()
    before = state(OutboundMail, mail.pk)

    with row_locked_elsewhere(OutboundMail, mail.pk), short_lock_timeout():
        OUTBOUND_MAIL.deliver(mail.pk)

    assert state(OutboundMail, mail.pk) == before
    assert len(contended_lines(pyracast_logs, OUTBOUND_MAIL.actor, mail.pk)) == 1
    assert outbox.pending_mail_ids(timezone.now()) == [mail.pk]


def test_a_metering_delivery_behind_a_held_region_lock_ends_without_claiming(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system, _, _ = connected(monkeypatch)
    EpiasCollectionJob.objects.update(available_at=NOW, retry_until=None)
    job = EpiasCollectionJob.objects.order_by("pk").first()
    assert job is not None
    before = state(EpiasCollectionJob, job.pk)

    with row_locked_elsewhere(Region, system.region.pk), short_lock_timeout():
        METERING.deliver(job.pk)

    assert state(EpiasCollectionJob, job.pk) == before
    assert len(contended_lines(pyracast_logs, METERING.actor, job.pk)) == 1
    assert job.pk in pending_epias_ids(NOW)


def test_a_revalidation_delivery_behind_a_held_account_lock_keeps_its_dispatch_lease(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system = make_system(approved=True)
    operations.replace_api_key(
        system.admin, system.account.pk, provider_key("NEW"), expected_revision=1
    )
    adapters: list[object] = []
    monkeypatch.setattr("pyracast.providers.adapter_for", adapters.append)
    lease = ProviderAccount.objects.values_list("revalidation_due_at", flat=True).get(
        pk=system.account.pk
    )
    assert lease is not None

    with row_locked_elsewhere(ProviderAccount, system.account.pk), short_lock_timeout():
        deliver_revalidation(system.account.pk, lease.isoformat())

    assert adapters == []
    assert state(ProviderAccount, system.account.pk, ("revalidation_due_at",)) == (lease,)
    assert len(contended_lines(pyracast_logs, REVALIDATION.actor, system.account.pk)) == 1
    assert f"account_id={system.account.pk} outcome=contended" in pyracast_logs.text
    assert claim_due_revalidations(lease) == [system.account.pk]


@pytest.mark.parametrize(
    "cause", [psycopg.errors.LockNotAvailable(), psycopg.errors.DeadlockDetected()]
)
def test_a_claim_that_waited_too_long_or_lost_a_deadlock_leaves_the_job_unclaimed(
    cause: Exception, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    def claim() -> object:
        raise database_error(cause)

    assert claim_unless_contended(COLLECTION.actor, 7, claim) is None
    assert contended_lines(pyracast_logs, COLLECTION.actor, 7) == [
        f"job_claim_contended kind=collect_forecast job_id=7 error_type={type(cause).__name__}"
    ]


@pytest.mark.parametrize("cause", [psycopg.errors.QueryCanceled(), None])
def test_a_claim_that_failed_for_another_database_reason_still_raises(
    cause: Exception | None, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    error = database_error(cause)

    def claim() -> object:
        raise error

    with pytest.raises(OperationalError) as raised:
        claim_unless_contended(COLLECTION.actor, 7, claim)
    assert raised.value is error
    assert contended_lines(pyracast_logs, COLLECTION.actor, 7) == []


def test_a_lock_timeout_after_a_calibration_claim_still_fails_its_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = request_run(*make_plant(monkeypatch))

    def locked(*args: object, **kwargs: object) -> object:
        raise database_error(psycopg.errors.LockNotAvailable())

    monkeypatch.setattr(training, "build_calibration_dataset", locked)

    with pytest.raises(OperationalError):
        CALIBRATION.deliver(run.pk)

    status, attempts, token = CalibrationRun.objects.values_list(
        "status", "attempts", "lease_token"
    ).get(pk=run.pk)
    assert (status, attempts, token is not None) == ("running", 1, True)
