from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail

from pyracast.accounts.models import Membership
from pyracast.accounts.operations import remove_workspace_member
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationSettings,
    ReminderSchedule,
)
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.overview import plants_at_recipient_limit
from pyracast.notifications.recipients import effective_recipients
from pyracast.notifications.scheduler import enqueue_due
from pyracast.regions.operations import submit_region
from tests.support.domain import NOW, System
from tests.support.notification_groups import group, member
from tests.support.notifications import (
    SLOT,
    addresses,
    configured,
    reminder_schedule_payload,
    revoked,
    store_recipients,
    stored_addresses,
)

pytestmark = pytest.mark.django_db(transaction=True)


def test_sent_history_revokes_an_address_that_another_delivery_pinned_as_external() -> None:
    system, _ = configured()
    schedule = group(system, ["old@example.com", "retained@example.com"])
    pending = NotificationDelivery.objects.get(pk=enqueue_due(SLOT)[0])
    assert not pending.recipients.filter(membership_id__isnull=False).exists()
    user = member(system, "old@example.com")
    membership = Membership.objects.get(tenant=system.tenant, user=user)
    historical = NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        scheduled_for=SLOT + timedelta(minutes=1),
        preparation_deadline=SLOT + timedelta(minutes=1),
        available_at=SLOT,
        status="sent",
        sent_at=SLOT + timedelta(minutes=1),
    )
    store_recipients(historical, [user.email], {user.email: [membership.pk]})
    user.email = "new@example.com"
    user.save(update_fields=["email"])
    EmailAddress.objects.filter(user=user).delete()
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    remove_workspace_member(system.member, system.tenant.pk, user.pk)
    pending.refresh_from_db()
    historical.refresh_from_db()
    schedule.refresh_from_db()
    assert revoked(pending) == ["old@example.com"]
    assert stored_addresses(pending) == ["old@example.com", "retained@example.com"]
    assert addresses(schedule) == ["retained@example.com"]
    assert historical.status == "sent"
    assert stored_addresses(historical) == ["old@example.com"]
    assert revoked(historical) == []

    Membership.objects.create(tenant=system.tenant, user=user)
    user.email = "old@example.com"
    user.save(update_fields=["email"])
    EmailAddress.objects.create(user=user, email=user.email, verified=True)
    assert set(effective_recipients(schedule)) == {"old@example.com", "retained@example.com"}
    assert process_notification(pending.pk, SLOT)
    assert mail.outbox[0].bcc == ["retained@example.com"]


def test_later_schedule_alias_evidence_prunes_earlier_external_schedule() -> None:
    system, _ = configured()
    first = group(system, ["OLD@example.com", "retained@example.com"])
    assert not first.recipients.filter(membership_id__isnull=False).exists()
    user = member(system, "old@example.com")
    membership = Membership.objects.get(tenant=system.tenant, user=user)
    region = submit_region(
        system.member,
        system.tenant.pk,
        name="Alias evidence",
        latitude=41,
        longitude=30,
        timezone="Europe/Istanbul",
    )
    evidence = ReminderSchedule.objects.create(region=region, effective_from=NOW)
    store_recipients(evidence, [user.email], {user.email: [membership.pk]})
    user.email = "new@example.com"
    user.save(update_fields=["email"])
    EmailAddress.objects.filter(user=user).delete()
    EmailAddress.objects.create(user=user, email=user.email, verified=True)
    remove_workspace_member(system.member, system.tenant.pk, user.pk)
    first.refresh_from_db()
    evidence.refresh_from_db()
    assert addresses(first) == ["retained@example.com"]
    assert effective_recipients(first) == ["retained@example.com"]
    assert addresses(evidence) == []


def test_historical_alias_does_not_revoke_a_different_pinned_member() -> None:
    system, _ = configured()
    former = member(system, "former@example.com")
    former_membership = Membership.objects.get(tenant=system.tenant, user=former)
    current = member(system, "shared@example.com")
    current_membership = Membership.objects.get(tenant=system.tenant, user=current)
    schedule = group(system, [current.email])
    pending = NotificationDelivery.objects.get(pk=enqueue_due(SLOT)[0])
    historical = NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        scheduled_for=SLOT - timedelta(days=1),
        preparation_deadline=SLOT - timedelta(days=1),
        available_at=SLOT - timedelta(days=1),
        status="sent",
    )
    store_recipients(historical, [current.email], {current.email: [former_membership.pk]})
    remove_workspace_member(system.member, system.tenant.pk, former.pk)
    schedule.refresh_from_db()
    pending.refresh_from_db()
    historical.refresh_from_db()
    assert list(schedule.recipients.values_list("email", "membership_id")) == [
        (current.email, current_membership.pk)
    ]
    assert pending.recipients.get(email=current.email).membership_id == current_membership.pk
    assert revoked(pending) == [former.email]
    assert stored_addresses(historical) == [current.email]
    assert revoked(historical) == []
    assert process_notification(pending.pk, SLOT)
    assert mail.outbox[0].bcc == [current.email]


def two_members_with_one_address(system: System) -> tuple[User, User]:
    """`shared@example.com` is the verified address of one member and the account address of
    another."""
    verified = member(system, "shared@example.com")
    account = User.objects.create(username="account-holder", email="SHARED@example.com")
    Membership.objects.create(tenant=system.tenant, user=account)
    return verified, account


def save_shared(system: System, *, include_workspace_members: bool = False) -> ReminderSchedule:
    return configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["shared@example.com"],
        include_workspace_members=include_workspace_members,
        times=["10:00"],
        language="en",
        now=NOW,
    )


def test_an_address_of_two_members_is_mailed_until_both_ties_end() -> None:
    system, _ = configured()
    first, second = two_members_with_one_address(system)
    save_shared(system)

    def mailed(day: int) -> list[str]:
        slot = SLOT + timedelta(days=day)
        before = len(mail.outbox)
        process_notification(enqueue_due(slot)[-1], slot)
        return [address for message in mail.outbox[before:] for address in message.bcc]

    assert mailed(0) == ["shared@example.com"]
    User.objects.filter(pk=first.pk).update(is_active=False)
    assert mailed(1) == ["shared@example.com"]
    User.objects.filter(pk=first.pk).update(is_active=True)
    remove_workspace_member(system.member, system.tenant.pk, first.pk)
    assert mailed(2) == ["shared@example.com"]
    remove_workspace_member(system.member, system.tenant.pk, second.pk)
    assert mailed(3) == []


def test_an_address_of_two_members_counts_once() -> None:
    system, _ = configured()
    two_members_with_one_address(system)
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 1})
    schedule = save_shared(system, include_workspace_members=True)
    assert process_notification(enqueue_due(SLOT)[-1], SLOT)

    viewer = reminder_schedule_payload(schedule, SLOT, administrator=False)
    administrator = reminder_schedule_payload(schedule, SLOT)
    assert (viewer["state"], viewer["recipient_count"], viewer["recipient_limit_exceeded"]) == (
        "sending",
        1,
        False,
    )
    assert viewer["deliveries"][0]["recipient_count"] == 1
    assert administrator["addresses"]["recipients"] == ["shared@example.com"]
    assert administrator["deliveries"][0]["recipients"] == ["shared@example.com"]
    NotificationSettings.objects.update(max_recipients=2)
    assert plants_at_recipient_limit(system.tenant.pk) == 0
