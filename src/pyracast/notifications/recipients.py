from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from django.db.models import Case, F, Q, Value, When

from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.notifications.addressing import Candidate, roles, sent_hidden
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    NotificationDeliveryStatus,
    RecipientRole,
    ReminderMemberRole,
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
class Person:
    """One membership with its account: `verified` holds the keys of its verified addresses."""

    membership: int
    administrator: bool
    active: bool
    primary: str
    verified: frozenset[str]


@dataclass(frozen=True)
class Member:
    """A verified primary address of active members, in the spelling of the lowest membership,
    with every membership whose address has its key, ascending."""

    address: str
    memberships: tuple[int, ...]
    administrator: bool

    @property
    def membership(self) -> int:
        return self.memberships[0]


@dataclass(frozen=True)
class Workspace:
    """Active memberships, the members of each address by key, and who holds each address:
    the memberships, active or not, with the address as a verified or primary one."""

    active: frozenset[int]
    by_key: Mapping[str, Member]
    holders: Mapping[str, frozenset[int]]

    @property
    def members(self) -> tuple[Member, ...]:
        return tuple(self.by_key.values())

    @property
    def addresses(self) -> tuple[str, ...]:
        return tuple(member.address for member in self.by_key.values())

    def memberships(self, address: str) -> frozenset[int]:
        found = self.by_key.get(recipient_key(address))
        return frozenset(found.memberships) if found else frozenset()


NO_MEMBERS = Workspace(frozenset(), {}, {})


def workspace(tenant_id: int, *, leaving: int | None = None) -> Workspace:
    return workspaces([tenant_id], leaving=leaving)[tenant_id]


def workspaces(tenant_ids: Iterable[int], *, leaving: int | None = None) -> dict[int, Workspace]:
    """Every membership of the workspaces with its account read in one statement. An address
    confirmation locks the account and not the workspace, so statements read apart could mix
    two states of one person; and callers hold the workspace, which may not wait for a person."""
    ids = set(tenant_ids)
    rows = Membership.objects.filter(tenant_id__in=ids)
    if leaving is not None:
        rows = rows.exclude(pk=leaving)
    people: dict[int, dict[int, Person]] = {pk: {} for pk in ids}
    for tenant_id, pk, role, active, primary, address, verified in rows.order_by(
        "pk", "user__emailaddress__pk"
    ).values_list(
        "tenant_id",
        "pk",
        "role",
        "user__is_active",
        "user__email",
        "user__emailaddress__email",
        "user__emailaddress__verified",
    ):
        known = people[tenant_id].get(pk) or Person(
            pk, role == WorkspaceRole.ADMIN, active, primary, frozenset()
        )
        if verified:
            known = replace(known, verified=known.verified | {recipient_key(address)})
        people[tenant_id][pk] = known
    return {tenant_id: _workspace(list(found.values())) for tenant_id, found in people.items()}


def _workspace(people: Sequence[Person]) -> Workspace:
    grouped: dict[str, list[Person]] = {}
    holders: dict[str, set[int]] = defaultdict(set)
    for person in people:
        key = recipient_key(person.primary)
        if person.active and key in person.verified:
            grouped.setdefault(key, []).append(person)
        for held in {key, *person.verified} - {""}:
            holders[held].add(person.membership)
    return Workspace(
        frozenset(person.membership for person in people if person.active),
        {
            key: Member(
                found[0].primary.strip(),
                tuple(person.membership for person in found),
                any(person.administrator for person in found),
            )
            for key, found in grouped.items()
        },
        {key: frozenset(found) for key, found in holders.items()},
    )


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


@dataclass(frozen=True)
class Resolved:
    """A schedule's typed rows, the workspace read with them and the addresses they permit."""

    explicit: list[Explicit]
    members: Workspace
    addresses: list[str]


def resolve(schedule: ReminderSchedule) -> Resolved:
    explicit = explicit_recipients([schedule.pk]).get(schedule.pk or 0, [])
    include = schedule.include_workspace_members
    members = (
        workspace(schedule.region.tenant_id)
        if needs_members(include, explicit) or not sent_hidden(schedule.hide_recipients)
        else NO_MEMBERS
    )
    return Resolved(explicit, members, effective(include, explicit, members))


def effective_recipients(schedule: ReminderSchedule) -> list[str]:
    return resolve(schedule).addresses


@dataclass(frozen=True)
class Choices:
    typed: dict[str, RecipientRole]
    chosen: dict[int, RecipientRole]


def stored_choices(schedule: ReminderSchedule) -> Choices:
    """The typed rows' choices by key and the members' choices by membership."""
    typed = ReminderRecipient.objects.filter(schedule_id=schedule.pk).exclude(role="")
    chosen = ReminderMemberRole.objects.filter(schedule_id=schedule.pk)
    return Choices(
        {key: RecipientRole(role) for key, role in typed.values_list("key", "role")},
        {pk: RecipientRole(role) for pk, role in chosen.values_list("membership_id", "role")},
    )


def candidates(
    addresses: Sequence[str], explicit: Sequence[Explicit], members: Workspace, choices: Choices
) -> list[Candidate]:
    """A typed row is settled while untied or while an active member it is tied to holds its
    address; the choice of an unsettled row counts only when it hides."""
    rows = {recipient_key(row.address): row for row in explicit}
    found = []
    for address in addresses:
        key = recipient_key(address)
        row, member = rows.get(key), members.by_key.get(key)
        found.append(
            Candidate(
                address,
                member=member is not None,
                administrator=member is not None and member.administrator,
                chosen=choices.chosen.get(member.membership) if member else None,
                typed=choices.typed.get(key) if row else None,
                settled=row is None
                or not row.ties
                or bool(row.ties & members.active & members.holders.get(key, frozenset())),
            )
        )
    return found


def placements(
    schedule: ReminderSchedule, resolved: Resolved, addresses: Sequence[str]
) -> list[RecipientRole]:
    """The header of each address of the list; a schedule sent hidden reads no choice."""
    hidden = sent_hidden(schedule.hide_recipients)
    found = (
        [Candidate(address) for address in addresses]
        if hidden
        else candidates(
            addresses,
            resolved.explicit,
            resolved.members,
            stored_choices(schedule),
        )
    )
    return roles(
        found,
        hide_recipients=hidden,
        include_workspace_members=schedule.include_workspace_members,
    )


def planned_recipients(
    members: Workspace, stored: Sequence[Explicit], addresses: Sequence[str]
) -> list[Explicit]:
    """Each address keeps its ties and gains those of the members who hold it now, so an email
    change of its member never makes it an external address."""
    ties = {recipient_key(row.address): row.ties for row in stored}
    return [
        Explicit(
            address,
            ties.get(key := recipient_key(address), frozenset())
            | members.holders.get(key, frozenset()),
        )
        for address in addresses
    ]


def recipient_roles(schedule: ReminderSchedule) -> dict[str, str]:
    """The choice of each typed row by key, `""` for none."""
    return dict(
        ReminderRecipient.objects.filter(schedule=schedule)
        .order_by("pk")
        .values_list("key", "role")
    )


def store_recipients(
    schedule: ReminderSchedule,
    stored: Sequence[Explicit],
    planned: Sequence[Explicit],
    *,
    roles: Mapping[str, str],
    stored_roles: Mapping[str, str],
) -> None:
    """`roles` holds the choice of each planned row by key; it is carried into the rebuilt rows."""
    if list(stored) == list(planned) and roles == stored_roles:
        return
    ReminderRecipient.objects.filter(schedule=schedule).delete()
    rows = ReminderRecipient.objects.bulk_create(
        ReminderRecipient(
            schedule=schedule,
            address=row.address,
            key=(key := recipient_key(row.address)),
            role=roles.get(key, ""),
        )
        for row in planned
    )
    ReminderRecipientTie.objects.bulk_create(
        ReminderRecipientTie(recipient=stored_row, membership_id=membership_id)
        for stored_row, row in zip(rows, planned, strict=True)
        for membership_id in sorted(row.ties)
    )


def store_member_choices(
    schedule: ReminderSchedule, choices: Mapping[int, RecipientRole | None]
) -> bool:
    """Stores or, for `None`, deletes the choice of each named membership; whether any
    changed."""
    rows = ReminderMemberRole.objects.filter(schedule=schedule)
    stored = dict(rows.filter(membership_id__in=choices).values_list("membership_id", "role"))
    deleted = [pk for pk, role in choices.items() if role is None and pk in stored]
    changed = {pk: role for pk, role in choices.items() if role and stored.get(pk) != role}
    if deleted:
        rows.filter(membership_id__in=deleted).delete()
    ReminderMemberRole.objects.bulk_create(
        (
            ReminderMemberRole(schedule=schedule, membership_id=pk, role=role)
            for pk, role in changed.items()
        ),
        update_conflicts=True,
        unique_fields=("schedule", "membership"),
        update_fields=("role",),
    )
    return bool(deleted or changed)


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


def record_roles(
    delivery: NotificationDelivery, addresses: Sequence[str], placed: Sequence[RecipientRole]
) -> None:
    """Writes every remaining row's header of this attempt, so a retry never keeps an earlier
    attempt's role."""
    keys = {
        role: [
            recipient_key(address)
            for address, given in zip(addresses, placed, strict=True)
            if given == role
        ]
        for role in (RecipientRole.TO, RecipientRole.CC)
    }
    DeliveryRecipient.objects.filter(delivery=delivery).update(
        role=Case(
            *(When(key__in=found, then=Value(role)) for role, found in keys.items() if found),
            default=Value(RecipientRole.BCC),
        )
    )


def delivery_addresses(delivery: NotificationDelivery) -> list[str]:
    """The envelope once the transmit marker is set, otherwise the rows not revoked."""
    return [
        row.address
        for row in delivery.recipient_rows.all()
        if delivery.transmit_started_at is not None or not row.revoked
    ]


def delivery_roles(delivery: NotificationDelivery) -> list[tuple[str, RecipientRole]] | None:
    """Each envelope address with its header, once the transmit marker is set."""
    if delivery.transmit_started_at is None:
        return None
    return [(row.address, RecipientRole(row.role)) for row in delivery.recipient_rows.all()]


def remove_member_recipients(tenant_id: int, membership_id: int, emails: set[str]) -> None:
    list(
        Region.objects.select_for_update()
        .filter(tenant_id=tenant_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    rows = ReminderRecipient.objects.filter(schedule__region__tenant_id=tenant_id)
    keys = {key for address in emails if (key := recipient_key(address))}
    keys.update(rows.filter(ties__membership_id=membership_id).values_list("key", flat=True))
    removed = rows.filter(
        Q(ties__membership_id=membership_id) | Q(ties__isnull=True, key__in=keys)
    ).values("pk")
    changed = set(
        ReminderRecipient.objects.filter(pk__in=removed).values_list("schedule_id", flat=True)
    )
    ReminderRecipient.objects.filter(pk__in=removed).delete()
    ReminderSchedule.objects.filter(pk__in=changed).update(revision=F("revision") + 1)
    _revoke_unauthorized(tenant_id, membership_id, keys)


def _revoke_unauthorized(tenant_id: int, leaving: int, keys: set[str]) -> None:
    """Revokes the open deliveries' rows of these keys that their schedule no longer authorizes
    without the leaving membership; an address another active member holds stays."""
    candidates = list(
        DeliveryRecipient.objects.filter(
            delivery__schedule__region__tenant_id=tenant_id,
            delivery__status__in=OPEN_DELIVERIES,
            key__in=keys,
        ).values_list(
            "pk",
            "key",
            "delivery__schedule_id",
            "delivery__schedule__include_workspace_members",
        )
    )
    if not candidates:
        return
    schedules = {schedule_id: include for _, _, schedule_id, include in candidates}
    explicit = explicit_recipients(schedules)
    members = workspace(tenant_id, leaving=leaving)
    authorized = {
        (schedule_id, recipient_key(address))
        for schedule_id, include in schedules.items()
        for address in effective(include, explicit.get(schedule_id, []), members)
    }
    DeliveryRecipient.objects.filter(
        pk__in=[
            pk for pk, key, schedule_id, _ in candidates if (schedule_id, key) not in authorized
        ]
    ).update(revoked=True)
