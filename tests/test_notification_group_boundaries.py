from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.db import connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor

from pyracast.accounts.models import Membership
from pyracast.accounts.operations import remove_workspace_member
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.scheduler import enqueue_due
from pyracast.trash import purge_expired, trash_region
from tests.support.domain import NOW
from tests.support.notifications import SLOT, configured, ready
from tests.support.trash_boundaries import _race_before_region_lock

pytestmark = pytest.mark.django_db(transaction=True)


def test_removing_a_verified_alias_preserves_other_members_and_external_addresses() -> None:
    system, _ = configured()
    removed = User.objects.create(username="removed", email="removed@example.com")
    retained = User.objects.create(username="retained", email="retained@example.com")
    blank = User.objects.create(username="blank", email="")
    removed_membership = Membership.objects.create(tenant=system.tenant, user=removed)
    retained_membership = Membership.objects.create(tenant=system.tenant, user=retained)
    Membership.objects.create(tenant=system.tenant, user=blank)
    EmailAddress.objects.create(user=removed, email="alias@example.com", verified=True)
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["ALIAS@example.com", retained.email, "external@example.com"],
        times=["10:00"],
        language="en",
        now=NOW,
    )
    (delivery_id,) = enqueue_due(SLOT)
    assert schedule.recipient_memberships == {
        "alias@example.com": [removed_membership.pk],
        retained.email: [retained_membership.pk],
    }
    remove_workspace_member(system.member, system.tenant.pk, removed.pk)
    schedule.refresh_from_db()
    assert schedule.recipients == [retained.email, "external@example.com"]
    assert schedule.recipient_memberships == {retained.email: [retained_membership.pk]}
    assert process_notification(delivery_id, SLOT)
    assert mail.outbox[0].bcc == [retained.email, "external@example.com"]


def test_migration_links_verified_aliases_once_and_preserves_empty_legacy_recipients() -> None:
    system, schedule = configured()
    primary = User.objects.create(username="primary", email="primary@example.com")
    primary_member = Membership.objects.create(tenant=system.tenant, user=primary)
    EmailAddress.objects.create(user=primary, email=primary.email, verified=True, primary=True)
    EmailAddress.objects.create(user=primary, email="alias@example.com", verified=True)
    EmailAddress.objects.create(user=primary, email="unverified@example.com", verified=False)
    blank = User.objects.create(username="blank", email="")
    blank_member = Membership.objects.create(tenant=system.tenant, user=blank)
    EmailAddress.objects.create(user=blank, email="blank-alias@example.com", verified=True)
    ReminderSchedule.objects.filter(pk=schedule.pk).update(
        recipients=[
            primary.email,
            "ALIAS@example.com",
            "blank-alias@example.com",
            "unverified@example.com",
        ]
    )
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    previous = [("notifications", "0008_shared_reminder_schedules")]
    executor.migrate(previous)
    try:
        deliveries = executor.loader.project_state(previous).apps.get_model(
            "notifications", "NotificationDelivery"
        )
        alias = deliveries.objects.create(
            schedule_id=schedule.pk,
            scheduled_for=SLOT,
            available_at=SLOT,
            recipient="ALIAS@example.com",
            status="uncertain",
            transmit_started_at=SLOT,
        )
        empty = deliveries.objects.create(
            schedule_id=schedule.pk,
            scheduled_for=SLOT,
            available_at=SLOT,
            recipient="",
            status="skipped",
        )
    finally:
        MigrationExecutor(connection).migrate(latest)
    schedule.refresh_from_db()
    assert schedule.recipient_memberships == {
        primary.email: [primary_member.pk],
        "alias@example.com": [primary_member.pk],
        "blank-alias@example.com": [blank_member.pk],
    }
    restored = NotificationDelivery.objects.get(pk=alias.pk)
    assert restored.recipients == ["ALIAS@example.com"]
    assert restored.recipient_memberships == {"alias@example.com": [primary_member.pk]}
    assert restored.status == "uncertain"
    assert restored.transmit_started_at == SLOT
    restored_empty = NotificationDelivery.objects.get(pk=empty.pk)
    assert restored_empty.recipients == []
    assert restored_empty.recipient_memberships == {}
    assert restored_empty.status == "skipped"


def test_rollback_rejects_duplicate_legacy_envelopes_without_altering_history() -> None:
    _, schedule = configured()
    for status in ("sent", "uncertain"):
        NotificationDelivery.objects.create(
            schedule=schedule,
            scheduled_for=SLOT,
            available_at=SLOT,
            recipients=["same@example.com"],
            grouped=False,
            status=status,
            attempts=2,
            transmit_started_at=SLOT,
            prepared_message={"body": status},
        )
    original = list(NotificationDelivery.objects.order_by("pk").values())
    with pytest.raises(IrreversibleError, match="cannot be represented"), transaction.atomic():
        MigrationExecutor(connection).migrate([("notifications", "0008_shared_reminder_schedules")])
    assert list(NotificationDelivery.objects.order_by("pk").values()) == original


def test_retention_purge_between_delivery_lookup_and_region_lock_cancels_transmission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()
    schedule = delivery.schedule
    actor = schedule.configured_by
    assert actor is not None
    trash_region(actor, schedule.region.tenant_id, schedule.region_id)
    schedule.region.refresh_from_db()
    deleted_at = schedule.region.deleted_at
    assert deleted_at is not None

    def purge() -> None:
        purge_expired(deleted_at + timedelta(days=31))

    assert (
        _race_before_region_lock(
            monkeypatch,
            "first",
            lambda: process_notification(delivery.pk, SLOT),
            purge,
        )
        is False
    )
    assert not mail.outbox
