from collections.abc import Iterable
from itertools import chain

from allauth.account.models import EmailAddress
from django.db.models import Q
from django.db.models.functions import Lower

from pyracast.accounts.models import Membership
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    NotificationDeliveryStatus,
    ReminderRecipient,
    ReminderSchedule,
)
from pyracast.regions.models import Region


def workspace_recipients(tenant_id: int) -> list[str]:
    verified = {
        (user_id, email.casefold())
        for user_id, email in EmailAddress.objects.filter(
            verified=True, user__membership__tenant_id=tenant_id
        ).values_list("user_id", "email")
    }
    return list(
        dict.fromkeys(
            email.casefold()
            for user_id, email in Membership.objects.filter(
                tenant_id=tenant_id, user__is_active=True
            )
            .order_by("pk")
            .values_list("user_id", "user__email")
            if (user_id, email.casefold()) in verified
        )
    )


def distinct_addresses(addresses: Iterable[str]) -> list[str]:
    named: dict[str, str] = {}
    for address in addresses:
        named.setdefault(address.casefold(), address)
    return list(named.values())


def address_memberships(tenant_id: int) -> dict[str, list[int]]:
    """Every membership each member address belongs to: as a verified address, then as an
    account address."""
    members = dict(Membership.objects.filter(tenant_id=tenant_id).values_list("user_id", "pk"))
    verified = (
        EmailAddress.objects.filter(user_id__in=members, verified=True)
        .order_by("pk")
        .values_list("user_id", "email")
    )
    accounts = (
        Membership.objects.filter(tenant_id=tenant_id)
        .order_by("pk")
        .values_list("user_id", "user__email")
    )
    result: dict[str, list[int]] = {}
    for user_id, address in chain(verified, accounts):
        if key := address.strip().casefold():
            tied = result.setdefault(key, [])
            if members[user_id] not in tied:
                tied.append(members[user_id])
    return result


def _pinned(rows: Iterable[ReminderRecipient]) -> dict[str, list[int]]:
    pinned: dict[str, list[int]] = {}
    for row in rows:
        if row.membership_id is not None:
            pinned.setdefault(row.email.casefold(), []).append(row.membership_id)
    return pinned


def active_memberships(tenant_id: int) -> set[int]:
    return set(
        Membership.objects.filter(tenant_id=tenant_id, user__is_active=True).values_list(
            "pk", flat=True
        )
    )


def stored_recipients(schedule: ReminderSchedule) -> list[str]:
    return distinct_addresses(row.email for row in schedule.recipients.all()) if schedule.pk else []


def effective_recipients(schedule: ReminderSchedule) -> list[str]:
    rows = list(schedule.recipients.all()) if schedule.pk else []
    pinned = any(row.membership_id is not None for row in rows)
    active = active_memberships(schedule.region.tenant_id) if pinned else set()
    addresses = {
        row.email.casefold(): row.email
        for row in rows
        if row.membership_id is None or row.membership_id in active
    }
    if schedule.include_workspace_members:
        for address in workspace_recipients(schedule.region.tenant_id):
            addresses.setdefault(address.casefold(), address)
    return list(addresses.values())


def _ties(schedule: ReminderSchedule, addresses: list[str]) -> list[tuple[str, int | None]]:
    """Each address with every membership it belongs to: the stored ones, else those of the
    members who hold it now, else none."""
    known = address_memberships(schedule.region.tenant_id)
    known.update(_pinned(schedule.recipients.all()) if schedule.pk else {})
    return [
        (address, membership)
        for address in addresses
        for membership in known.get(address.casefold()) or (None,)
    ]


def recipient_rows(schedule: ReminderSchedule, addresses: list[str]) -> list[ReminderRecipient]:
    return [
        ReminderRecipient(schedule=schedule, email=address, membership_id=membership)
        for address, membership in _ties(schedule, addresses)
    ]


def delivery_rows(
    delivery: NotificationDelivery, schedule: ReminderSchedule, addresses: list[str]
) -> list[DeliveryRecipient]:
    return [
        DeliveryRecipient(delivery=delivery, email=address, membership_id=membership)
        for address, membership in _ties(schedule, addresses)
    ]


def permitted_recipients(delivery: NotificationDelivery) -> list[str]:
    current = {address.casefold() for address in effective_recipients(delivery.schedule)}
    active = active_memberships(delivery.schedule.region.tenant_id)
    return distinct_addresses(
        row.email
        for row in delivery.recipients.all()
        if not row.revoked
        and row.email.casefold() in current
        and (row.membership_id is None or row.membership_id in active)
    )


def remove_member_recipients(tenant_id: int, membership_id: int, emails: set[str]) -> None:
    list(
        Region.objects.select_for_update()
        .filter(tenant_id=tenant_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    pinned_schedules = ReminderRecipient.objects.filter(
        schedule__region__tenant_id=tenant_id, membership_id=membership_id
    )
    pinned_deliveries = DeliveryRecipient.objects.filter(
        delivery__schedule__region__tenant_id=tenant_id, membership_id=membership_id
    )
    addresses = {address.lower() for address in emails}
    addresses.update(pinned_schedules.values_list(Lower("email"), flat=True))
    addresses.update(pinned_deliveries.values_list(Lower("email"), flat=True))
    member = Q(membership_id=membership_id) | Q(membership_id__isnull=True, lowered__in=addresses)
    ReminderRecipient.objects.filter(schedule__region__tenant_id=tenant_id).alias(
        lowered=Lower("email")
    ).filter(member).delete()
    DeliveryRecipient.objects.filter(
        delivery__schedule__region__tenant_id=tenant_id,
        delivery__status__in=(
            NotificationDeliveryStatus.PENDING,
            NotificationDeliveryStatus.RETRY,
            NotificationDeliveryStatus.SENDING,
            NotificationDeliveryStatus.UNCERTAIN,
        ),
    ).alias(lowered=Lower("email")).filter(member).update(revoked=True)
