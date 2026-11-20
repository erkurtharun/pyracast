from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from allauth.account.models import EmailAddress
from django.db.models import F, Q

from pyracast.accounts.models import Membership
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    NotificationDeliveryStatus,
    ReminderRecipient,
    ReminderRecipientTie,
    ReminderSchedule,
)
from pyracast.regions.models import Region

OPEN_DELIVERIES = (
    NotificationDeliveryStatus.PENDING,
    NotificationDeliveryStatus.RETRY,
    NotificationDeliveryStatus.SENDING,
    NotificationDeliveryStatus.UNCERTAIN,
)


def recipient_key(address: str) -> str:
    return address.strip().casefold()


@dataclass(frozen=True)
class Explicit:
    """An address the administrator entered, eligible while untied or while any tie is active."""

    address: str
    ties: frozenset[int]


@dataclass(frozen=True)
class Workspace:
    active: frozenset[int]
    addresses: tuple[str, ...]


NO_MEMBERS = Workspace(frozenset(), ())


def workspace(tenant_id: int) -> Workspace:
    """Active memberships and the verified primary addresses of their members, read now."""
    members = list(
        Membership.objects.filter(tenant_id=tenant_id, user__is_active=True)
        .order_by("pk")
        .values_list("pk", "user_id", "user__email")
    )
    verified = {
        (user_id, recipient_key(email))
        for user_id, email in EmailAddress.objects.filter(
            verified=True, user__membership__tenant_id=tenant_id
        ).values_list("user_id", "email")
    }
    addresses: dict[str, str] = {}
    for _, user_id, email in members:
        key = recipient_key(email)
        if (user_id, key) in verified:
            addresses.setdefault(key, email.strip())
    return Workspace(frozenset(pk for pk, _, _ in members), tuple(addresses.values()))


def grouped_recipients(
    rows: Iterable[tuple[int, int, str, int | None]],
) -> dict[int, list[Explicit]]:
    """Rows of (schedule id, recipient id, address, tied membership id or None), one per tie."""
    found: dict[int, tuple[int, str, set[int]]] = {}
    for schedule_id, pk, address, membership_id in rows:
        ties = found.setdefault(pk, (schedule_id, address, set()))[2]
        if membership_id is not None:
            ties.add(membership_id)
    grouped: dict[int, list[Explicit]] = defaultdict(list)
    for schedule_id, address, ties in found.values():
        grouped[schedule_id].append(Explicit(address, frozenset(ties)))
    return grouped


def explicit_recipients(schedule_ids: Iterable[int | None]) -> dict[int, list[Explicit]]:
    return grouped_recipients(
        ReminderRecipient.objects.filter(schedule_id__in=[pk for pk in schedule_ids if pk])
        .order_by("pk")
        .values_list("schedule_id", "pk", "address", "ties__membership_id")
    )


def needs_members(include_workspace_members: bool, explicit: Sequence[Explicit]) -> bool:
    return include_workspace_members or any(row.ties for row in explicit)


def effective(
    include_workspace_members: bool, explicit: Sequence[Explicit], members: Workspace
) -> list[str]:
    addresses: dict[str, str] = {}
    for row in explicit:
        if not row.ties or members.active & row.ties:
            addresses.setdefault(recipient_key(row.address), row.address)
    if include_workspace_members:
        for address in members.addresses:
            addresses.setdefault(recipient_key(address), address)
    return list(addresses.values())


def effective_recipients(schedule: ReminderSchedule) -> list[str]:
    explicit = explicit_recipients([schedule.pk]).get(schedule.pk or 0, [])
    include = schedule.include_workspace_members
    if not needs_members(include, explicit):
        return effective(include, explicit, NO_MEMBERS)
    return effective(include, explicit, workspace(schedule.region.tenant_id))


def address_holders(tenant_id: int) -> dict[str, set[int]]:
    """The memberships, active or not, whose member holds each address now."""
    memberships = dict(Membership.objects.filter(tenant_id=tenant_id).values_list("user_id", "pk"))
    addresses = list(
        EmailAddress.objects.filter(user_id__in=memberships, verified=True).values_list(
            "user_id", "email"
        )
    )
    addresses.extend(
        Membership.objects.filter(tenant_id=tenant_id).values_list("user_id", "user__email")
    )
    holders: dict[str, set[int]] = defaultdict(set)
    for user_id, address in addresses:
        if key := recipient_key(address):
            holders[key].add(memberships[user_id])
    return holders


def planned_recipients(
    tenant_id: int, stored: Sequence[Explicit], addresses: Sequence[str]
) -> list[Explicit]:
    """Each address keeps its ties and gains those of the members who hold it now, so an email
    change of its member never makes it an external address."""
    ties = {recipient_key(row.address): row.ties for row in stored}
    holders = address_holders(tenant_id)
    return [
        Explicit(
            address,
            ties.get(key := recipient_key(address), frozenset()) | holders.get(key, set()),
        )
        for address in addresses
    ]


def store_recipients(
    schedule: ReminderSchedule, stored: Sequence[Explicit], planned: Sequence[Explicit]
) -> None:
    if list(stored) == list(planned):
        return
    ReminderRecipient.objects.filter(schedule=schedule).delete()
    rows = ReminderRecipient.objects.bulk_create(
        ReminderRecipient(schedule=schedule, address=row.address, key=recipient_key(row.address))
        for row in planned
    )
    ReminderRecipientTie.objects.bulk_create(
        ReminderRecipientTie(recipient=stored_row, membership_id=membership_id)
        for stored_row, row in zip(rows, planned, strict=True)
        for membership_id in sorted(row.ties)
    )


def freeze_recipients(delivery: NotificationDelivery, addresses: Sequence[str]) -> None:
    DeliveryRecipient.objects.bulk_create(
        DeliveryRecipient(delivery=delivery, address=address, key=recipient_key(address))
        for address in addresses
    )


def narrow_recipients(delivery: NotificationDelivery, current: Sequence[str]) -> list[str]:
    """Deletes the candidate rows that are revoked or no longer current; rows never return."""
    keys = {recipient_key(address) for address in current}
    rows = list(DeliveryRecipient.objects.filter(delivery=delivery).order_by("pk"))
    kept = [row for row in rows if not row.revoked and row.key in keys]
    if excluded := [row.pk for row in rows if row not in kept]:
        DeliveryRecipient.objects.filter(pk__in=excluded).delete()
    return [row.address for row in kept]


def delivery_addresses(delivery: NotificationDelivery) -> list[str]:
    """The envelope once the transmit marker is set, otherwise the rows not revoked."""
    return [
        row.address
        for row in delivery.recipient_rows.all()
        if delivery.transmit_started_at is not None or not row.revoked
    ]


def remove_member_recipients(tenant_id: int, membership_id: int, emails: set[str]) -> None:
    list(
        Region.objects.select_for_update()
        .filter(tenant_id=tenant_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    rows = ReminderRecipient.objects.filter(schedule__region__tenant_id=tenant_id)
    keys = {recipient_key(address) for address in emails}
    keys.update(rows.filter(ties__membership_id=membership_id).values_list("key", flat=True))
    removed = rows.filter(
        Q(ties__membership_id=membership_id) | Q(ties__isnull=True, key__in=keys)
    ).values("pk")
    changed = set(
        ReminderRecipient.objects.filter(pk__in=removed).values_list("schedule_id", flat=True)
    )
    ReminderRecipient.objects.filter(pk__in=removed).delete()
    ReminderSchedule.objects.filter(pk__in=changed).update(revision=F("revision") + 1)
    DeliveryRecipient.objects.filter(
        delivery__schedule__region__tenant_id=tenant_id,
        delivery__status__in=OPEN_DELIVERIES,
        key__in=keys,
    ).update(revoked=True)
