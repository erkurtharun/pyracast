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


def address_memberships(tenant_id: int) -> dict[str, int]:
    """The membership each member address belongs to; a verified address wins over the
    account address of another member."""
    members = dict(Membership.objects.filter(tenant_id=tenant_id).values_list("user_id", "pk"))
    result: dict[str, int] = {}
    for user_id, address in (
        EmailAddress.objects.filter(user_id__in=members, verified=True)
        .order_by("pk")
        .values_list("user_id", "email")
    ):
        result.setdefault(address.strip().casefold(), members[user_id])
    for user_id, address in (
        Membership.objects.filter(tenant_id=tenant_id)
        .order_by("pk")
        .values_list("user_id", "user__email")
    ):
        if key := address.strip().casefold():
            result.setdefault(key, members[user_id])
    return result


def active_memberships(tenant_id: int) -> set[int]:
    return set(
        Membership.objects.filter(tenant_id=tenant_id, user__is_active=True).values_list(
            "pk", flat=True
        )
    )


def stored_recipients(schedule: ReminderSchedule) -> list[str]:
    return [row.email for row in schedule.recipients.all()] if schedule.pk else []


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


def recipient_rows(schedule: ReminderSchedule, addresses: list[str]) -> list[ReminderRecipient]:
    """Rows for a saved list: an address keeps its stored membership, and otherwise belongs to
    the member who holds it now."""
    pinned = {
        row.email.casefold(): row.membership_id
        for row in schedule.recipients.all()
        if row.membership_id is not None
    }
    known = address_memberships(schedule.region.tenant_id)
    return [
        ReminderRecipient(
            schedule=schedule,
            email=address,
            membership_id=pinned.get(address.casefold(), known.get(address.casefold())),
        )
        for address in addresses
    ]


def delivery_rows(
    delivery: NotificationDelivery, schedule: ReminderSchedule, addresses: list[str]
) -> list[DeliveryRecipient]:
    known = address_memberships(schedule.region.tenant_id)
    known.update(
        (row.email.casefold(), row.membership_id)
        for row in schedule.recipients.all()
        if row.membership_id is not None
    )
    return [
        DeliveryRecipient(
            delivery=delivery, email=address, membership_id=known.get(address.casefold())
        )
        for address in addresses
    ]


def permitted_recipients(delivery: NotificationDelivery) -> list[str]:
    current = {address.casefold() for address in effective_recipients(delivery.schedule)}
    active = active_memberships(delivery.schedule.region.tenant_id)
    return [
        row.email
        for row in delivery.recipients.all()
        if not row.revoked
        and row.email.casefold() in current
        and (row.membership_id is None or row.membership_id in active)
    ]


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
