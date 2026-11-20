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
    candidates,
    configured,
    freeze,
    reminder_schedule_payload,
    revoked,
    schedule_addresses,
    store,
    ties,
)

pytestmark = pytest.mark.django_db(transaction=True)


def test_another_schedule_tied_to_the_member_prunes_the_same_address_kept_as_external() -> None:
    system, _ = configured()
    first = group(system, ["OLD@example.com", "retained@example.com"])
    assert ties(first) == {}
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
    store(evidence, [user.email], {user.email: [membership.pk]})
    user.email = "new@example.com"
    user.save(update_fields=["email"])
    EmailAddress.objects.filter(user=user).delete()
    EmailAddress.objects.create(user=user, email=user.email, verified=True)
    remove_workspace_member(system.member, system.tenant.pk, user.pk)
    assert schedule_addresses(first) == ["retained@example.com"]
    assert effective_recipients(first) == ["retained@example.com"]
    assert schedule_addresses(evidence) == []
    assert ties(evidence) == {}


def test_removing_a_member_keeps_an_address_tied_to_another_member() -> None:
    system, _ = configured()
    former = member(system, "former@example.com")
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
    freeze(historical, [current.email, former.email])
    remove_workspace_member(system.member, system.tenant.pk, former.pk)
    assert schedule_addresses(schedule) == [current.email]
    assert ties(schedule) == {current.email: [current_membership.pk]}
    assert revoked(pending) == [former.email]
    assert (candidates(historical), revoked(historical)) == ([current.email, former.email], [])
    assert process_notification(pending.pk, SLOT)
    assert mail.outbox[0].bcc == [current.email]


def two_members_with_one_address(system: System) -> tuple[User, User]:
    """`shared@example.com` is the verified address of one member and the account address of
    another."""
    verified = member(system, "shared@example.com")
    account = User.objects.create(username="account-holder", email="SHARED@example.com")
    Membership.objects.create(tenant=system.tenant, user=account)
    return verified, account


def test_an_address_of_two_members_counts_once() -> None:
    system, _ = configured()
    two_members_with_one_address(system)
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 1})
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["shared@example.com"],
        include_workspace_members=True,
        times=["10:00"],
        language="en",
        now=NOW,
    )
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


def test_an_address_pinned_to_an_inactive_member_reaches_the_member_holding_it_now() -> None:
    system, _ = configured()
    former = member(system, "shared@example.com")
    schedule = group(system, ["shared@example.com"])
    assert ties(schedule) == {"shared@example.com": [Membership.objects.get(user=former).pk]}
    User.objects.filter(pk=former.pk).update(is_active=False, email="former@example.com")
    EmailAddress.objects.filter(user=former).delete()
    holder = User.objects.create(username="holder", email="shared@example.com")
    Membership.objects.create(tenant=system.tenant, user=holder)
    EmailAddress.objects.create(
        user=holder, email="shared@example.com", verified=True, primary=True
    )
    assert effective_recipients(schedule) == ["shared@example.com"]

    (delivery_id,) = enqueue_due(SLOT)
    process_notification(delivery_id, SLOT)

    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert (delivery.status, delivery.last_error) == ("sent", "")
    assert mail.outbox[0].bcc == ["shared@example.com"]
