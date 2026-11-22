import uuid
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from django.core import mail
from django.core.management import get_commands
from django.db.models import QuerySet
from django.utils import timezone

from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import OutboundMail
from pyracast.background.deadlines import LEASE
from pyracast.connections.contracts import ProviderData
from pyracast.epias import EpiasDay, EpiasPlant
from pyracast.forecasts.jobs.claim import Failure, claim, retry
from pyracast.forecasts.jobs.collect import persist_snapshot
from pyracast.forecasts.jobs.ready import pending_job_ids
from pyracast.forecasts.models import CollectionJob, CollectionJobError
from pyracast.metering import jobs as metering_jobs
from pyracast.metering.jobs import claim_epias_job, pending_epias_ids, run_epias_job
from pyracast.metering.models import EpiasCollectionJob, EpiasSnapshot
from pyracast.notifications import outbox
from pyracast.notifications.jobs import pending_notification_ids, process_notification
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.operations import configure_reminders
from pyracast.plants import training
from pyracast.plants.models import CalibrationRun
from pyracast.plants.training import pending_calibration_ids
from pyracast.trash import trash_region
from tests.support.concurrency import behind_open_transaction
from tests.support.domain import NOW, System, make_system
from tests.support.invariants import lease_violations
from tests.support.metering import connected, day_data
from tests.support.notification_transmission import OpeningBackend, replying
from tests.support.notifications import SLOT, candidates, ready
from tests.support.outbox import pending_mail_ids
from tests.support.plant_operations import make_plant, request_run

pytestmark = pytest.mark.django_db(transaction=True)

SECOND = timedelta(seconds=1)
EXPIRED = NOW + LEASE + SECOND
# Claims add the monotonic time spent waiting for their locks to the time they were given.
WAITED = timedelta(seconds=30)
UNAVAILABLE = {
    "format": "plain",
    "kind": "unavailable",
    "subject": "Pyracast forecast unavailable",
    "body": "Tomorrow's saved forecast could not be prepared.",
    "prepared_at": NOW.isoformat(),
}


def lease_of(rows: QuerySet[Any]) -> tuple[str, int, uuid.UUID | None]:
    status, attempts, token = rows.values_list("status", "attempts", "lease_token").get()
    return status, attempts, token


def expires_one_lease_after(rows: QuerySet[Any], moment: datetime) -> bool:
    expires = rows.values_list("lease_expires_at", flat=True).get()
    return expires is not None and moment + LEASE <= expires < moment + LEASE + WAITED


def test_two_workers_cannot_both_hold_one_collection_job() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.filter(pk=CollectionJob.objects.get().pk)

    held, refused = behind_open_transaction(
        lambda: claim(job.get().pk, NOW), lambda: claim(job.get().pk, NOW)
    )

    assert held is not None
    assert refused is None
    assert lease_of(job) == ("running", 1, held.token)
    assert expires_one_lease_after(job, NOW)


def test_an_interrupted_collection_job_is_reclaimed_and_its_first_owner_is_ignored() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.filter(pk=CollectionJob.objects.get().pk)
    first = claim(job.get().pk, NOW)
    assert first is not None

    assert pending_job_ids(NOW + LEASE - SECOND) == []
    assert pending_job_ids(EXPIRED) == [first.job_id]
    second = claim(first.job_id, EXPIRED)

    assert second is not None
    assert second.token != first.token
    assert persist_snapshot(first, ProviderData({}, ()), EXPIRED) is None
    retry(first, EXPIRED, Failure(CollectionJobError.PROVIDER_FAILURE))
    assert lease_of(job) == ("running", 2, second.token)
    assert expires_one_lease_after(job, EXPIRED)
    assert lease_violations()["forecasts_collectionjob"] == 0


def epias_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[System, EpiasCollectionJob, EpiasCollectionJob]:
    system, _, _ = connected(monkeypatch)
    EpiasCollectionJob.objects.update(available_at=NOW, retry_until=None)
    first, second = EpiasCollectionJob.objects.order_by("pk")[:2]
    return system, first, second


def answer(job: EpiasCollectionJob) -> EpiasDay:
    return day_data(EpiasPlant(**job.provider_plant), job.target_date)


def test_two_workers_cannot_both_hold_one_epias_job(monkeypatch: pytest.MonkeyPatch) -> None:
    _, stored, _ = epias_jobs(monkeypatch)
    job = EpiasCollectionJob.objects.filter(pk=stored.pk)

    held, refused = behind_open_transaction(
        lambda: claim_epias_job(stored.pk, NOW), lambda: claim_epias_job(stored.pk, NOW)
    )

    assert held is not None
    assert refused is None
    assert lease_of(job) == ("running", 1, held.token)
    assert expires_one_lease_after(job, NOW)


def test_an_interrupted_epias_job_is_reclaimed_and_its_first_owner_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, stored, _ = epias_jobs(monkeypatch)
    job = EpiasCollectionJob.objects.filter(pk=stored.pk)
    first = claim_epias_job(stored.pk, NOW)
    assert first is not None

    assert stored.pk not in pending_epias_ids(NOW + LEASE - SECOND)
    assert stored.pk in pending_epias_ids(EXPIRED)
    second = claim_epias_job(stored.pk, EXPIRED)

    assert second is not None
    assert second.token != first.token
    assert not metering_jobs._publish(first, answer(stored), EXPIRED)
    assert lease_of(job) == ("running", 2, second.token)
    assert expires_one_lease_after(job, EXPIRED)
    assert not EpiasSnapshot.objects.exists()


def test_trash_supersedes_the_waiting_and_running_epias_jobs_of_its_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, waiting, running = epias_jobs(monkeypatch)
    lease = claim_epias_job(running.pk, NOW)
    assert lease is not None
    trash_region(system.member, system.tenant.pk, system.region.pk)
    requested: list[date] = []

    def fetch(plant: EpiasPlant, day: date) -> EpiasDay:
        requested.append(day)
        return day_data(plant, day)

    assert not run_epias_job(waiting.pk, fetch=fetch, now=NOW)
    assert not metering_jobs._publish(lease, answer(running), NOW)

    assert requested == []
    assert lease_of(EpiasCollectionJob.objects.filter(pk=waiting.pk)) == ("superseded", 0, None)
    assert lease_of(EpiasCollectionJob.objects.filter(pk=running.pk)) == ("superseded", 1, None)
    assert not EpiasSnapshot.objects.exists()
    assert lease_violations()["metering_epiascollectionjob"] == 0


def test_two_workers_cannot_both_hold_one_calibration_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = request_run(*make_plant(monkeypatch))
    run = CalibrationRun.objects.filter(pk=stored.pk)

    held, refused = behind_open_transaction(
        lambda: training._claim(stored.pk, NOW), lambda: training._claim(stored.pk, NOW)
    )

    assert held is not None
    assert refused is None
    assert lease_of(run) == ("running", 1, held.token)
    assert expires_one_lease_after(run, NOW)


def test_an_interrupted_calibration_run_is_reclaimed_and_its_first_owner_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = request_run(*make_plant(monkeypatch))
    run = CalibrationRun.objects.filter(pk=stored.pk)
    first = training._claim(stored.pk, NOW)
    assert first is not None

    assert pending_calibration_ids(NOW + LEASE - SECOND) == []
    assert pending_calibration_ids(EXPIRED) == [stored.pk]
    second = training._claim(stored.pk, EXPIRED)

    assert second is not None
    assert second.token != first.token
    training._fail(first, EXPIRED)
    assert lease_of(run) == ("running", 2, second.token)
    assert expires_one_lease_after(run, EXPIRED)


def test_two_workers_cannot_both_send_one_reminder() -> None:
    delivery = ready()

    held, refused = behind_open_transaction(
        lambda: process_notification(delivery.pk, SLOT),
        lambda: process_notification(delivery.pk, SLOT),
    )

    assert (held, refused) == (True, False)
    assert [list(message.bcc) for message in mail.outbox] == [["default@example.com"]]
    assert lease_of(NotificationDelivery.objects.filter(pk=delivery.pk)) == ("sent", 1, None)


@pytest.mark.parametrize(
    ("marked", "outcome"),
    [
        (False, ("retry", "interrupted_connection", False)),
        (True, ("uncertain", "interrupted_send", True)),
    ],
)
def test_an_interrupted_reminder_is_recovered_by_its_transmit_marker(
    marked: bool, outcome: tuple[str, str, bool]
) -> None:
    delivery = ready()
    token = uuid.uuid4()
    rows = NotificationDelivery.objects.filter(pk=delivery.pk)
    rows.update(
        prepared_message=UNAVAILABLE,
        status="sending",
        attempts=1,
        lease_token=token,
        lease_expires_at=SLOT + LEASE,
        transmit_started_at=SLOT if marked else None,
    )
    later = SLOT + LEASE + timedelta(seconds=1)

    assert pending_notification_ids(SLOT + LEASE - SECOND) == []
    assert pending_notification_ids(later) == [delivery.pk]
    assert not process_notification(delivery.pk, later)

    recovered = rows.get()
    assert (recovered.status, recovered.last_error, recovered.lease_token == token) == outcome
    assert recovered.attempts == 1
    if not marked:
        assert recovered.lease_expires_at is None
        assert later <= recovered.available_at < later + WAITED
    assert not mail.outbox
    assert lease_violations()["notifications_notificationdelivery"] == 0


def test_a_reminder_narrowed_to_no_recipient_before_its_marker_releases_its_lease() -> None:
    delivery = ready()
    rows = NotificationDelivery.objects.filter(pk=delivery.pk)
    schedule = delivery.schedule
    administrator = schedule.configured_by
    assert administrator is not None

    def replace_recipients() -> None:
        configure_reminders(
            administrator,
            schedule.region.tenant_id,
            schedule.region_id,
            enabled=True,
            recipients=["replacement@example.com"],
            times=["10:00"],
            language="en",
            now=NOW,
        )

    with replying(lambda message: None) as sent:
        OpeningBackend.action = replace_recipients
        try:
            assert not process_notification(delivery.pk, SLOT)
        finally:
            OpeningBackend.action = None

    assert sent == []
    stored = rows.get()
    assert (stored.status, stored.last_error) == ("skipped", "no_recipients")
    assert lease_of(rows) == ("skipped", 1, None)
    assert stored.lease_expires_at is None
    assert stored.transmit_started_at is None
    assert candidates(stored) == []
    assert pending_notification_ids(SLOT + LEASE + SECOND) == []
    assert lease_violations()["notifications_notificationdelivery"] == 0


def outbound_mail() -> OutboundMail:
    system = make_system()
    invite_workspace_member(system.member, system.tenant.pk, "guest@example.com")
    return OutboundMail.objects.get()


def test_two_workers_cannot_both_hold_one_outbound_mail() -> None:
    mail = outbound_mail()
    now = timezone.now()

    held, refused = behind_open_transaction(
        lambda: outbox._claim(mail.pk, now), lambda: outbox._claim(mail.pk, now)
    )

    assert held is not None
    assert refused is None
    stored = OutboundMail.objects.filter(pk=mail.pk)
    assert lease_of(stored) == ("sending", 1, held[1])
    assert stored.get().lease_expires_at == now + LEASE


def test_an_interrupted_outbound_mail_is_released_before_it_is_claimed_again() -> None:
    mail = outbound_mail()
    now = timezone.now()
    first = outbox._claim(mail.pk, now)
    assert first is not None
    later = now + LEASE + timedelta(seconds=1)
    stored = OutboundMail.objects.filter(pk=mail.pk)

    assert pending_mail_ids(now + LEASE - SECOND) == []
    assert pending_mail_ids(later) == [mail.pk]
    assert outbox._claim(mail.pk, later) is None
    assert lease_of(stored) == ("retry", 1, None)
    assert stored.get().last_error == "interrupted_connection"
    second = outbox._claim(mail.pk, later)

    assert second is not None
    assert second[1] != first[1]
    assert lease_of(stored) == ("sending", 2, second[1])
    assert stored.get().lease_expires_at == later + LEASE


def test_the_background_commands_keep_their_names() -> None:
    assert {"worker", "dispatch", "check_delivery_health"} <= set(get_commands())
