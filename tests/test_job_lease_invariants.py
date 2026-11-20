import uuid
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import pytest
from django.core.mail import EmailMessage
from django.db import IntegrityError, models, transaction

from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import Membership
from pyracast.epias import EpiasDay, EpiasError, EpiasPlant
from pyracast.forecasts.jobs.claim import LEASE_DURATION, claim
from pyracast.forecasts.jobs.ready import pending_job_ids
from pyracast.forecasts.models import CollectionJob
from pyracast.forecasts.schedule import configure_schedule
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.metering.jobs import claim_epias_job, pending_epias_ids, run_epias_job
from pyracast.metering.models import EpiasCollectionJob
from pyracast.metering.operations import disconnect_epias
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import NotificationDelivery, OutboundMail
from pyracast.plants import training
from pyracast.plants.models import CalibrationRun
from pyracast.plants.training import pending_calibration_ids, run_calibration
from pyracast.regions.models import Region
from pyracast.trash import trash_region
from tests.support.domain import NOW, FakeAdapter, make_system
from tests.support.invariants import lease_violations
from tests.support.jobs import calculate, collect
from tests.support.metering import connected, day_data
from tests.support.notification_transmission import replying
from tests.support.notifications import SLOT, ready
from tests.support.plant_operations import make_plant, request_run

pytestmark = pytest.mark.django_db(transaction=True)

CLEAN = dict.fromkeys(
    (
        "forecasts_collectionjob",
        "metering_epiascollectionjob",
        "plants_calibrationrun",
        "notifications_notificationdelivery",
        "notifications_outboundmail",
    ),
    0,
)
EXPIRED = NOW + LEASE_DURATION + timedelta(seconds=1)
UNAVAILABLE = {
    "format": "plain",
    "kind": "unavailable",
    "subject": "Pyracast forecast unavailable",
    "body": "Tomorrow's saved forecast could not be prepared.",
    "prepared_at": NOW.isoformat(),
}


def _collection_job(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    make_system(approved=True)
    return CollectionJob.objects.get()


def _epias_job(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None
    return job


def _calibration_run(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return request_run(*make_plant(monkeypatch))


def _delivery(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return _prepared(ready())


def _outbound_mail(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    system = make_system()
    invite_workspace_member(system.member, system.tenant.pk, "guest@example.com")
    return OutboundMail.objects.get()


def _prepared(delivery: NotificationDelivery) -> NotificationDelivery:
    NotificationDelivery.objects.filter(pk=delivery.pk).update(prepared_message=UNAVAILABLE)
    return delivery


ROWS: list[tuple[Callable[[pytest.MonkeyPatch], models.Model], str, str]] = [
    (_collection_job, "running", "collectionjob_lease_chk"),
    (_epias_job, "running", "epiascollectionjob_lease_chk"),
    (_calibration_run, "running", "calibrationrun_lease_chk"),
    (_delivery, "sending", "notificationdelivery_lease_chk"),
    (_outbound_mail, "sending", "outboundmail_lease_chk"),
]
LEASE: dict[str, Any] = {"lease_token": uuid.uuid4(), "lease_expires_at": NOW}


@pytest.mark.parametrize(("row", "leased", "constraint"), ROWS)
@pytest.mark.parametrize(
    "write",
    [
        "leased_without_lease",
        "leased_without_expiry",
        "leased_without_token",
        "idle_with_lease",
        "idle_with_token",
    ],
)
def test_lease_columns_agree_with_the_status_on_every_raw_write(
    row: Callable[[pytest.MonkeyPatch], models.Model],
    leased: str,
    constraint: str,
    write: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = row(monkeypatch)
    writes: dict[str, dict[str, Any]] = {
        "leased_without_lease": {"status": leased},
        "leased_without_expiry": {"status": leased, "lease_token": uuid.uuid4()},
        "leased_without_token": {"status": leased, "lease_expires_at": NOW},
        "idle_with_lease": LEASE,
        "idle_with_token": {"lease_token": uuid.uuid4()},
    }
    values = writes[write]
    rows = type(stored)._default_manager.filter(pk=stored.pk)

    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        rows.update(**values)

    rows.update(status=leased, **LEASE)
    assert lease_violations() == CLEAN


def test_an_uncertain_delivery_keeps_or_drops_its_whole_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = NotificationDelivery.objects.filter(pk=_prepared(ready()).pk)
    rows.update(status="uncertain", **LEASE)
    rows.update(lease_token=None, lease_expires_at=None)
    with (
        pytest.raises(IntegrityError, match="notificationdelivery_lease_chk"),
        transaction.atomic(),
    ):
        rows.update(lease_token=uuid.uuid4())
    assert lease_violations() == CLEAN


def test_an_expired_collection_lease_on_a_superseded_binding_is_released_as_stale() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()
    assert claim(job.pk, NOW) is not None
    Region.objects.filter(pk=system.region.pk).update(status="suspended")

    assert claim(job.pk, EXPIRED) is None

    job.refresh_from_db()
    assert (job.status, job.lease_token, job.lease_expires_at) == ("stale", None, None)
    assert lease_violations() == CLEAN


def test_collection_lease_writers_keep_the_lease_with_the_running_status() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()
    assert claim(job.pk, NOW) is not None
    assert lease_violations() == CLEAN
    assert pending_job_ids(EXPIRED) == [job.pk]

    def fail() -> None:
        raise ValueError("provider down")

    assert not collect(job.pk, FakeAdapter(fail), calculate, EXPIRED)
    job.refresh_from_db()
    assert job.status == "retry"
    assert lease_violations() == CLEAN
    assert collect(job.pk, FakeAdapter(), calculate, job.available_at)
    assert lease_violations() == CLEAN

    waiting, running = (
        CollectionJob.objects.create(
            binding=system.binding,
            scheduled_for=NOW + timedelta(hours=hours),
            target_date=NOW.date(),
            available_at=NOW,
        )
        for hours in (1, 2)
    )
    assert claim(running.pk, NOW) is not None
    configure_schedule(
        system.member, system.tenant.pk, system.region.pk, enabled=False, times=[], now=NOW
    )
    trash_region(system.member, system.tenant.pk, system.region.pk)
    assert set(
        CollectionJob.objects.filter(pk__in=(waiting.pk, running.pk)).values_list(
            "status", flat=True
        )
    ) == {"stale"}
    assert lease_violations() == CLEAN


def test_the_scheduler_requeues_a_cancelled_current_slot_without_a_lease() -> None:
    system = make_system(approved=True)
    configure_schedule(
        system.member, system.tenant.pk, system.region.pk, enabled=True, times=["16:00"], now=NOW
    )
    enqueue_due(NOW)
    current = CollectionJob.objects.filter(endpoint="forecasts", scheduled_for=NOW)
    current.update(status="stale")

    assert current.get().pk in enqueue_due(NOW)

    assert current.values_list("status", "lease_token", "lease_expires_at").get() == (
        "pending",
        None,
        None,
    )
    assert lease_violations() == CLEAN


def test_metering_lease_writers_keep_the_lease_with_the_running_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = connected(monkeypatch)
    EpiasCollectionJob.objects.update(available_at=NOW, retry_until=None)
    first, second, third, fourth = EpiasCollectionJob.objects.order_by("pk")[:4]

    def fail(provider: EpiasPlant, day: date) -> EpiasDay:
        raise EpiasError("provider down")

    assert run_epias_job(first.pk, fetch=day_data, now=NOW)
    assert not run_epias_job(second.pk, fetch=fail, now=NOW)
    assert lease_violations() == CLEAN
    assert claim_epias_job(third.pk, NOW) is not None
    EpiasCollectionJob.objects.filter(pk=third.pk).update(retry_until=EXPIRED)
    assert third.pk not in pending_epias_ids(EXPIRED)
    third.refresh_from_db()
    assert third.status == "failed"
    assert lease_violations() == CLEAN

    assert claim_epias_job(fourth.pk, NOW) is not None
    disconnect_epias(plant.pk, actor=system.admin)
    fourth.refresh_from_db()
    assert fourth.status == "superseded"
    assert lease_violations() == CLEAN


def test_calibration_lease_writers_keep_the_lease_with_the_running_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    lease = training._claim(run.pk, NOW)
    assert lease is not None
    assert lease_violations() == CLEAN
    assert pending_calibration_ids(EXPIRED) == [run.pk]
    training._fail(lease, NOW)
    assert lease_violations() == CLEAN
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "insufficient"
    assert lease_violations() == CLEAN

    pending = CalibrationRun.objects.get(pk=request_run(system, plant).pk)
    assert training._claim(pending.pk, NOW) is not None
    trash_region(system.member, system.tenant.pk, system.region.pk)
    pending.refresh_from_db()
    assert pending.status == "superseded"
    assert lease_violations() == CLEAN


def _sent(message: EmailMessage) -> None:
    return None


def _interrupted(message: EmailMessage) -> None:
    raise ConnectionError("interrupted")


@pytest.mark.parametrize(("reply", "outcome"), [(_sent, "sent"), (_interrupted, "uncertain")])
def test_notification_lease_writers_keep_the_lease_with_the_sending_status(
    reply: Callable[[EmailMessage], None], outcome: str
) -> None:
    delivery = ready()
    with replying(reply):
        process_notification(delivery.pk, SLOT)
    delivery.refresh_from_db()
    assert delivery.status == outcome
    assert lease_violations() == CLEAN


def test_trash_releases_the_lease_of_a_sending_delivery() -> None:
    delivery = _prepared(ready())
    NotificationDelivery.objects.filter(pk=delivery.pk).update(status="sending", **LEASE)
    region = delivery.schedule.region
    admin = Membership.objects.get(tenant_id=region.tenant_id).user
    trash_region(admin, region.tenant_id, region.pk)
    delivery.refresh_from_db()
    assert delivery.status == "cancelled"
    assert lease_violations() == CLEAN
