import uuid
from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail

from pyracast.accounts.models import Membership
from pyracast.accounts.operations import remove_workspace_member
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    NotificationSettings,
    ReminderSchedule,
)
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.overview import plants_at_recipient_limit
from pyracast.notifications.recipients import (
    delivery_addresses,
    effective_recipients,
    recipient_key,
)
from pyracast.notifications.scheduler import enqueue_due
from pyracast.regions.operations import submit_region
from tests.support.domain import NOW, System, make_system
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


def tied_elsewhere(system: System, address: str, membership_id: int) -> ReminderSchedule:
    region = submit_region(
        system.member,
        system.tenant.pk,
        name="Alias evidence",
        latitude=41,
        longitude=30,
        timezone="Europe/Istanbul",
    )
    evidence = ReminderSchedule.objects.create(region=region, effective_from=NOW)
    store(evidence, [address], {recipient_key(address): [membership_id]})
    return evidence


def test_another_schedule_tied_to_the_member_prunes_the_same_address_kept_as_external() -> None:
    system, _ = configured()
    first = group(system, ["OLD@example.com", "retained@example.com"])
    assert ties(first) == {}
    user = member(system, "old@example.com")
    membership = Membership.objects.get(tenant=system.tenant, user=user)
    evidence = tied_elsewhere(system, user.email, membership.pk)
    user.email = "new@example.com"
    user.save(update_fields=["email"])
    EmailAddress.objects.filter(user=user).delete()
    EmailAddress.objects.create(user=user, email=user.email, verified=True)
    remove_workspace_member(system.member, system.tenant.pk, user.pk)
    assert schedule_addresses(first) == ["retained@example.com"]
    assert effective_recipients(first) == ["retained@example.com"]
    assert schedule_addresses(evidence) == []
    assert ties(evidence) == {}


@pytest.mark.parametrize("include_workspace_members", [False, True])
def test_an_address_tied_elsewhere_to_a_removed_member_still_reaches_the_member_holding_it(
    include_workspace_members: bool,
) -> None:
    system, _ = configured()
    former = member(system, "former@example.com")
    former_membership = Membership.objects.get(tenant=system.tenant, user=former)
    current = member(system, "shared@example.com")
    current_membership = Membership.objects.get(tenant=system.tenant, user=current)
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=[] if include_workspace_members else [current.email],
        include_workspace_members=include_workspace_members,
        times=["10:00"],
        language="en",
        now=NOW,
    )
    evidence = tied_elsewhere(system, current.email, former_membership.pk)
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

    assert schedule_addresses(evidence) == []
    assert ties(schedule) == (
        {} if include_workspace_members else {current.email: [current_membership.pk]}
    )
    assert revoked(pending) == ([former.email] if include_workspace_members else [])
    assert (candidates(historical), revoked(historical)) == ([current.email, former.email], [])
    assert process_notification(pending.pk, SLOT)
    assert mail.outbox[0].bcc == [current.email]


def test_a_member_without_an_address_removes_no_blank_recipient() -> None:
    system, schedule = configured()
    store(schedule, ["", "default@example.com"])
    blank = User.objects.create(username="blank", email="")
    Membership.objects.create(tenant=system.tenant, user=blank)
    revision = ReminderSchedule.objects.get(pk=schedule.pk).revision

    remove_workspace_member(system.member, system.tenant.pk, blank.pk)

    assert schedule_addresses(schedule) == ["", "default@example.com"]
    assert ReminderSchedule.objects.get(pk=schedule.pk).revision == revision


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


def test_the_list_leaves_out_an_address_tied_to_a_membership_that_does_not_exist() -> None:
    system = make_system(approved=True)
    held = Membership.objects.get(user=member(system, "holder@example.com")).pk
    schedule = ReminderSchedule.objects.create(
        region=system.region,
        effective_from=NOW,
        times=["10:00"],
        enabled=True,
        include_workspace_members=True,
    )
    store(
        schedule,
        ["Holder@Example.com", "gone@example.com", "external@example.com", "beyond@example.com"],
        {"holder@example.com": [held], "gone@example.com": [987654321], "beyond@example.com": [0]},
    )

    assert effective_recipients(schedule) == ["Holder@Example.com", "external@example.com"]


LEASE = {"lease_token": uuid.uuid4(), "lease_expires_at": SLOT}
PREPARED = {"format": "plain", "kind": "unavailable"}


def recorded_delivery(
    system: System, minute: int, status: str, stored: list[tuple[str, bool]], **values: object
) -> NotificationDelivery:
    slot = SLOT + timedelta(minutes=minute)
    schedule, _ = ReminderSchedule.objects.get_or_create(
        region=system.region, defaults={"effective_from": NOW}
    )
    delivery = NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        scheduled_for=slot,
        available_at=slot,
        preparation_deadline=slot,
        status=status,
        **values,
    )
    DeliveryRecipient.objects.bulk_create(
        DeliveryRecipient(
            delivery=delivery, address=address, key=recipient_key(address), revoked=gone
        )
        for address, gone in stored
    )
    return delivery


def test_a_delivery_marked_for_transmission_lists_its_whole_envelope_whatever_its_status() -> None:
    system = make_system(approved=True)
    marked = [
        recorded_delivery(
            system,
            minute,
            status,
            [("a@example.com", False), ("b@example.com", True)],
            transmit_started_at=SLOT,
            prepared_message=PREPARED,
            **(LEASE if status == "uncertain" else {}),
        )
        for minute, status in enumerate(("sent", "failed", "uncertain", "retry"))
    ]

    assert [delivery_addresses(delivery) for delivery in marked] == [
        ["a@example.com", "b@example.com"]
    ] * 4


@pytest.mark.parametrize(
    "status", ["pending", "retry", "sending", "uncertain", "skipped", "cancelled"]
)
def test_a_delivery_without_a_marker_lists_its_addresses_not_revoked_whatever_its_status(
    status: str,
) -> None:
    leased = status in {"sending", "uncertain"}
    delivery = recorded_delivery(
        make_system(approved=True),
        0,
        status,
        [("a@example.com", False), ("B@example.com", True)],
        prepared_message=PREPARED if leased else None,
        **(LEASE if leased else {}),
    )

    assert delivery_addresses(delivery) == ["a@example.com"]
