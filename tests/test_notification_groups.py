from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import monotonic
from typing import cast

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.test import override_settings
from rest_framework.test import APIClient

from pyracast.accounts.models import Membership, Tenant
from pyracast.accounts.operations import remove_workspace_member
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.notifications.recipients import effective_recipients
from pyracast.notifications.scheduler import enqueue_due
from tests.support.notification_groups import group, member
from tests.support.notification_transmission import OpeningBackend
from tests.support.notifications import SLOT, configured, reminder_schedule_payload

pytestmark = pytest.mark.django_db(transaction=True)


def test_one_bcc_message_with_deduplicated_verified_workspace_members() -> None:
    system, _ = configured()
    alice = member(system, "alice@example.com")
    bob = member(system, "bob@example.com")
    member(system, "unverified@example.com", verified=False)
    member(system, "inactive@example.com", active=False)
    outsider = User.objects.create(username="outsider", email="outsider@example.com")
    other = Tenant.objects.create(name="Other")
    Membership.objects.create(tenant=other, user=outsider)
    EmailAddress.objects.create(user=outsider, email=outsider.email, verified=True)
    schedule = group(system, ["ALICE@example.com", "external@example.com"])
    assert schedule.recipients == ["ALICE@example.com", "external@example.com"]
    assert set(effective_recipients(schedule)) == {
        "ALICE@example.com",
        bob.email,
        "external@example.com",
    }
    ids = enqueue_due(SLOT)
    assert len(ids) == 1
    assert enqueue_due(SLOT) == ids
    assert process_notification(ids[0], SLOT)
    assert not process_notification(ids[0], SLOT)
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == message.cc == []
    assert set(message.bcc) == {"ALICE@example.com", bob.email, "external@example.com"}
    assert message.message().get("Bcc") is None
    delivery = NotificationDelivery.objects.get(pk=ids[0])
    assert delivery.recipient_memberships[alice.email] == [Membership.objects.get(user=alice).pk]
    history = cast(list[dict[str, object]], reminder_schedule_payload(schedule, SLOT)["deliveries"])
    assert history[0]["recipients"] == delivery.recipients


def test_new_members_only_join_future_slots_and_removed_members_leave_pending_group() -> None:
    system, _ = configured()
    first = member(system, "first@example.com")
    group(system)
    key = enqueue_due(SLOT)[0]
    second = member(system, "second@example.com")
    assert enqueue_due(SLOT) == [key]
    remove_workspace_member(system.member, system.tenant.pk, first.pk)
    assert not process_notification(key, SLOT)
    assert not mail.outbox
    next_ids = enqueue_due(SLOT + timedelta(days=1))
    next_delivery = NotificationDelivery.objects.get(pk=next_ids[-1])
    assert next_delivery.recipients == [second.email]
    assert process_notification(next_delivery.pk, SLOT + timedelta(days=1))
    assert mail.outbox[0].bcc == [second.email]


def test_removal_during_connection_open_shrinks_the_actual_envelope() -> None:
    system, _ = configured()
    removed = member(system, "removed@example.com")
    retained = member(system, "retained@example.com")
    group(system, [removed.email])
    key = enqueue_due(SLOT)[0]
    OpeningBackend.sent = []
    OpeningBackend.action = lambda: remove_workspace_member(
        system.member, system.tenant.pk, removed.pk
    )
    try:
        with override_settings(
            MAILERS={
                "default": {"BACKEND": "tests.support.notification_transmission.OpeningBackend"}
            }
        ):
            assert process_notification(key, SLOT)
    finally:
        OpeningBackend.action = None
    assert len(OpeningBackend.sent) == 1
    assert OpeningBackend.sent[0].bcc == [retained.email]
    assert NotificationDelivery.objects.get(pk=key).recipients == [retained.email]
    assert ReminderSchedule.objects.get().recipients == []


def test_email_change_resave_and_removal_keep_the_original_member_link() -> None:
    system, _ = configured()
    user = member(system, "old@example.com")
    group(system, [user.email, "external@example.com"])
    user.email = "new@example.com"
    user.save(update_fields=["email"])
    EmailAddress.objects.filter(user=user).delete()
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    group(system, ["OLD@example.com", "external@example.com"])
    key = enqueue_due(SLOT)[0]
    remove_workspace_member(system.member, system.tenant.pk, user.pk)
    schedule = ReminderSchedule.objects.get()
    assert schedule.recipients == ["external@example.com"]
    assert process_notification(key, SLOT)
    assert mail.outbox[0].bcc == ["external@example.com"]
    assert NotificationDelivery.objects.get(pk=key).recipients == ["external@example.com"]


def test_removing_and_rejoining_cannot_revive_pinned_membership() -> None:
    system, _ = configured()
    user = member(system, "member@example.com")
    group(system)
    key = enqueue_due(SLOT)[0]
    remove_workspace_member(system.member, system.tenant.pk, user.pk)
    Membership.objects.create(tenant=system.tenant, user=user)
    assert not process_notification(key, SLOT)
    assert not mail.outbox


def paused_slot(schedule: ReminderSchedule) -> NotificationDelivery:
    delivery = NotificationDelivery.objects.get(schedule=schedule, scheduled_for=SLOT)
    assert delivery.status == "skipped"
    assert delivery.recipients == []
    assert delivery.recipient_memberships == {}
    assert delivery.snapshot is None
    return delivery


def test_member_growth_pauses_without_truncation_and_removal_resumes_next_slot() -> None:
    system, _ = configured()
    users = [member(system, f"member{i}@example.com") for i in range(5)]
    schedule = group(system)
    extra = member(system, "extra@example.com")
    assert len(effective_recipients(schedule)) == 6
    assert reminder_schedule_payload(schedule, SLOT)["recipient_limit_exceeded"] is True
    enqueue_due(SLOT)
    assert paused_slot(schedule).last_error == "recipient_limit_exceeded"
    with pytest.raises(ValidationError, match="at most 5"):
        group(system)
    remove_workspace_member(system.member, system.tenant.pk, extra.pk)
    next_slot = SLOT + timedelta(days=1)
    enqueue_due(next_slot)
    assert NotificationDelivery.objects.filter(schedule=schedule).count() == 2
    resumed = NotificationDelivery.objects.get(schedule=schedule, scheduled_for=next_slot)
    assert resumed.status == "pending"
    assert set(resumed.recipients) == {user.email for user in users}


def test_over_limit_slot_records_a_skipped_row() -> None:
    system, _ = configured()
    for index in range(6):
        member(system, f"member{index}@example.com")
    schedule = ReminderSchedule.objects.get()
    ReminderSchedule.objects.filter(pk=schedule.pk).update(include_workspace_members=True)
    ids = enqueue_due(SLOT)
    delivery = paused_slot(schedule)
    assert delivery.last_error == "recipient_limit_exceeded"
    assert ids == [delivery.pk]
    assert enqueue_due(SLOT) == [delivery.pk]
    assert NotificationDelivery.objects.count() == 1


def test_no_recipient_slot_records_a_skipped_row() -> None:
    _, schedule = configured()
    ReminderSchedule.objects.filter(pk=schedule.pk).update(recipients=[])
    enqueue_due(SLOT)
    assert paused_slot(schedule).last_error == "no_recipients"
    assert not process_notification(paused_slot(schedule).pk, SLOT)
    assert not mail.outbox


def test_member_removal_does_not_change_accepted_history_or_other_workspaces() -> None:
    system, _ = configured()
    user = member(system, "member@example.com")
    schedule = group(system, [user.email])
    key = enqueue_due(SLOT)[0]
    assert process_notification(key, SLOT)
    other = Tenant.objects.create(name="Other")
    other_membership = Membership.objects.create(tenant=other, user=user)
    remove_workspace_member(system.member, system.tenant.pk, user.pk)
    schedule.refresh_from_db()
    assert effective_recipients(schedule) == []
    assert NotificationDelivery.objects.get(pk=key).recipients == [user.email]
    assert Membership.objects.filter(pk=other_membership.pk).exists()


def test_group_setting_is_workspace_admin_only_and_viewers_can_read() -> None:
    system, _ = configured()
    viewer = member(system, "viewer@example.com")
    client = APIClient()
    client.force_authenticate(viewer)
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"
    payload = {
        "enabled": True,
        "include_workspace_members": True,
        "recipients": [],
        "times": ["10:00"],
        "language": "en",
    }
    assert client.put(url, payload, format="json").status_code == 403
    client.force_authenticate(system.member)
    response = client.put(url, payload, format="json")
    assert response.status_code == 200
    assert response.json()["recipient_count"] == 1
    assert response.json()["addresses"]["workspace_recipients"] == [viewer.email]
    client.force_authenticate(viewer)
    assert client.get(url).json()["include_workspace_members"] is True


def test_removal_and_group_transmission_serialize_on_workspace_lock() -> None:
    system, _ = configured()
    removed = member(system, "removed@example.com")
    retained = member(system, "retained@example.com")
    group(system)
    key = enqueue_due(SLOT)[0]
    removed_inside_transaction = Event()
    worker_entered = Event()
    commit_removal = Event()
    sent: list[list[str]] = []
    sending_pids: list[int] = []

    def remove() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                remove_workspace_member(system.member, system.tenant.pk, removed.pk)
                removed_inside_transaction.set()
                assert commit_removal.wait(10)
        finally:
            connections.close_all()

    def send() -> bool:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                sending_pids.append(cursor.fetchone()[0])
            worker_entered.set()
            return process_notification(
                key, SLOT, sender=lambda message: sent.append(list(message.bcc))
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        removing = pool.submit(remove)
        assert removed_inside_transaction.wait(10)
        sending = pool.submit(send)
        try:
            assert worker_entered.wait(10)
            deadline = monotonic() + 10
            while True:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_blocking_pids(%s)", [sending_pids[0]])
                    if cursor.fetchone()[0]:
                        break
                assert monotonic() < deadline, "Transmission did not wait for membership removal."
                commit_removal.wait(0.01)
        finally:
            commit_removal.set()
        removing.result(timeout=15)
        assert sending.result(timeout=15)
    assert sent == [[retained.email]]


def test_one_delivery_per_reminder_slot_prevents_duplicate_dispatch() -> None:
    system, schedule = configured()
    first = NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        scheduled_for=SLOT,
        preparation_deadline=SLOT,
        available_at=SLOT,
        recipients=["sent@example.com"],
        status="sent",
    )
    assert enqueue_due(SLOT) == [first.pk]
    assert not process_notification(first.pk, SLOT)
    with (
        pytest.raises(IntegrityError, match="notificationdelivery_schedule_scheduled_for_uniq"),
        transaction.atomic(),
    ):
        NotificationDelivery.objects.create(
            schedule=schedule,
            binding=system.binding,
            scheduled_for=SLOT,
            preparation_deadline=SLOT,
            available_at=SLOT,
            recipients=["duplicate@example.com"],
        )


def test_unverified_alias_cannot_claim_or_remove_an_external_recipient() -> None:
    system, _ = configured()
    user = member(system, "member@example.com")
    EmailAddress.objects.create(user=user, email="external@example.com", verified=False)
    schedule = group(system, ["external@example.com"])
    assert "external@example.com" not in schedule.recipient_memberships
    remove_workspace_member(system.member, system.tenant.pk, user.pk)
    schedule.refresh_from_db()
    assert effective_recipients(schedule) == ["external@example.com"]
    assert process_notification(enqueue_due(SLOT)[0], SLOT)
    assert mail.outbox[0].bcc == ["external@example.com"]


@pytest.mark.parametrize("change", ["inactive", "unverified", "new_email"])
def test_workspace_recipients_are_rechecked_before_transmission(change: str) -> None:
    system, _ = configured()
    user = member(system, "member@example.com")
    group(system)
    key = enqueue_due(SLOT)[0]
    if change == "inactive":
        user.is_active = False
        user.save(update_fields=["is_active"])
    elif change == "unverified":
        EmailAddress.objects.filter(user=user).update(verified=False)
    else:
        user.email = "new@example.com"
        user.save(update_fields=["email"])
        EmailAddress.objects.create(user=user, email=user.email, verified=True)
    assert not process_notification(key, SLOT)
    assert not mail.outbox
