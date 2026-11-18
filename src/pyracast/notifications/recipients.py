from itertools import chain

from allauth.account.models import EmailAddress

from pyracast.accounts.models import Membership
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryStatus,
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


def recipient_memberships(tenant_id: int) -> dict[str, list[int]]:
    memberships = dict(Membership.objects.filter(tenant_id=tenant_id).values_list("user_id", "pk"))
    addresses = list(
        EmailAddress.objects.filter(user_id__in=memberships, verified=True).values_list(
            "user_id", "email"
        )
    )
    addresses.extend(
        Membership.objects.filter(tenant_id=tenant_id).values_list("user_id", "user__email")
    )
    result: dict[str, list[int]] = {}
    for user_id, address in addresses:
        key = address.strip().casefold()
        member_id = memberships[user_id]
        if key and member_id not in result.setdefault(key, []):
            result[key].append(member_id)
    return result


def active_memberships(tenant_id: int) -> set[int]:
    return set(
        Membership.objects.filter(tenant_id=tenant_id, user__is_active=True).values_list(
            "pk", flat=True
        )
    )


def effective_recipients(schedule: ReminderSchedule) -> list[str]:
    if not isinstance(schedule.recipients, list) or any(
        not isinstance(address, str) for address in schedule.recipients
    ):
        raise TypeError("Reminder recipients must be a list of email addresses.")
    active = (
        active_memberships(schedule.region.tenant_id) if schedule.recipient_memberships else set()
    )
    addresses = {
        address.casefold(): address
        for address in schedule.recipients
        if not (members := schedule.recipient_memberships.get(address.casefold()))
        or active.intersection(members)
    }
    if schedule.include_workspace_members:
        for address in workspace_recipients(schedule.region.tenant_id):
            addresses.setdefault(address.casefold(), address)
    return list(addresses.values())


def pin_recipient_memberships(
    schedule: ReminderSchedule, addresses: list[str]
) -> dict[str, list[int]]:
    known = recipient_memberships(schedule.region.tenant_id)
    known.update(schedule.recipient_memberships)
    return {
        address.casefold(): known[address.casefold()]
        for address in addresses
        if address.casefold() in known
    }


def permitted_recipients(delivery: NotificationDelivery) -> list[str]:
    current = {address.casefold() for address in effective_recipients(delivery.schedule)}
    revoked = set(delivery.revoked_recipients)
    active = active_memberships(delivery.schedule.region.tenant_id)
    return [
        address
        for address in delivery.recipients
        if address.casefold() in current
        and address.casefold() not in revoked
        and (
            not (members := delivery.recipient_memberships.get(address.casefold()))
            or active.intersection(members)
        )
    ]


def _recipient_belongs_to_member(
    address: str,
    membership_id: int,
    known_addresses: set[str],
    pinned_memberships: dict[str, list[int]],
) -> bool:
    if members := pinned_memberships.get(address.casefold()):
        return membership_id in members
    return address.casefold() in known_addresses


def remove_member_recipients(tenant_id: int, membership_id: int, emails: set[str]) -> None:
    normalized = {address.casefold() for address in emails}
    list(
        Region.objects.select_for_update()
        .filter(tenant_id=tenant_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    schedule_maps = ReminderSchedule.objects.filter(region__tenant_id=tenant_id).values_list(
        "recipient_memberships", flat=True
    )
    delivery_maps = NotificationDelivery.objects.filter(
        schedule__region__tenant_id=tenant_id
    ).values_list("recipient_memberships", flat=True)
    normalized.update(
        address
        for mapping in chain(schedule_maps.iterator(), delivery_maps.iterator())
        for address, members in mapping.items()
        if membership_id in members
    )
    for schedule in ReminderSchedule.objects.select_for_update().filter(
        region__tenant_id=tenant_id
    ):
        removed = {
            address.casefold()
            for address in schedule.recipients
            if _recipient_belongs_to_member(
                address, membership_id, normalized, schedule.recipient_memberships
            )
        }
        schedule.recipients = [
            address for address in schedule.recipients if address.casefold() not in removed
        ]
        schedule.recipient_memberships = {
            address: members
            for address, members in schedule.recipient_memberships.items()
            if address not in removed
        }
        schedule.save(update_fields=("recipients", "recipient_memberships"))
    for delivery in NotificationDelivery.objects.select_for_update().filter(
        schedule__region__tenant_id=tenant_id,
        status__in=(
            NotificationDeliveryStatus.PENDING,
            NotificationDeliveryStatus.RETRY,
            NotificationDeliveryStatus.SENDING,
            NotificationDeliveryStatus.UNCERTAIN,
        ),
    ):
        revoked = set(delivery.revoked_recipients) | {
            address.casefold()
            for address in delivery.recipients
            if _recipient_belongs_to_member(
                address, membership_id, normalized, delivery.recipient_memberships
            )
        }
        delivery.revoked_recipients = sorted(revoked)
        delivery.save(update_fields=("revoked_recipients",))
