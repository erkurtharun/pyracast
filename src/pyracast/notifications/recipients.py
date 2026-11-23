from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from django.db.models import Case, F, Q, Value, When

from pyracast.accounts.addresses import is_plain
from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.notifications.addressing import Candidate, members_choice, roles, sent_hidden
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
    """The identity of an address: for a plain one, its lower-cased text."""
    return address.strip().casefold()


@dataclass(frozen=True)
class Explicit:
    """An address the administrator entered, eligible while untied or while any tie is active."""

    address: str
    ties: frozenset[int]


@dataclass(frozen=True)
class Person:
    membership: int
    administrator: bool
    active: bool
    primary: str
    verified: frozenset[str]


@dataclass(frozen=True)
class Member:
    """A verified primary address of active members, in the lowest membership's spelling."""

    address: str
    memberships: tuple[int, ...]
    administrator: bool


@dataclass(frozen=True)
class Workspace:
    """`holds_non_plain`: a person of the read, active or not, holds a primary or verified
    address that is not plain, so a list that reads these people is not sent."""

    active: frozenset[int]
    _members: Mapping[str, Member]
    _holders: Mapping[str, frozenset[int]]
    holds_non_plain: bool = False

    @property
    def members(self) -> tuple[Member, ...]:
        return tuple(self._members.values())

    @property
    def addresses(self) -> tuple[str, ...]:
        return tuple(member.address for member in self._members.values())

    def member(self, address: str) -> Member | None:
        return self._members.get(recipient_key(address))

    def memberships(self, address: str) -> frozenset[int]:
        found = self.member(address)
        return frozenset(found.memberships) if found else frozenset()

    def are_members(self, address: str, memberships: Collection[int]) -> bool:
        """Whether these are exactly the members of the address in this read of people."""
        return self.memberships(address) == frozenset(memberships)

    def holders(self, address: str) -> frozenset[int]:
        """The memberships, active or not, holding the address verified or as primary."""
        return self._holders.get(recipient_key(address), frozenset())


NO_MEMBERS = Workspace(frozenset(), {}, {})


def workspace(tenant_id: int, *, leaving: int | None = None) -> Workspace:
    return workspaces([tenant_id], leaving=leaving)[tenant_id]


def workspaces(tenant_ids: Iterable[int], *, leaving: int | None = None) -> dict[int, Workspace]:
    """One statement: an address confirmation locks the account, not the workspace callers hold."""
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
            known = replace(known, verified=known.verified | {address})
        people[tenant_id][pk] = known
    return {tenant_id: _workspace(list(found.values())) for tenant_id, found in people.items()}


def _workspace(people: Sequence[Person]) -> Workspace:
    grouped: dict[str, list[Person]] = {}
    holders: dict[str, set[int]] = defaultdict(set)
    for person in people:
        key = recipient_key(person.primary)
        if person.active and key in {recipient_key(address) for address in person.verified}:
            grouped.setdefault(key, []).append(person)
        for address in (person.primary, *person.verified):
            if address.strip():
                holders[recipient_key(address)].add(person.membership)
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
        not all(
            is_plain(address)
            for person in people
            for address in (person.primary, *person.verified)
            if address.strip()
        ),
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


def reads_people(
    include_workspace_members: bool, hide_recipients: bool, explicit: Sequence[Explicit]
) -> bool:
    """Whether the list depends on the workspace's people: for its members or for placement."""
    return needs_members(include_workspace_members, explicit) or not sent_hidden(hide_recipients)


def resolve(schedule: ReminderSchedule) -> Resolved:
    """`members` is `NO_MEMBERS` for a list that does not read people."""
    explicit = explicit_recipients([schedule.pk]).get(schedule.pk or 0, [])
    include = schedule.include_workspace_members
    members = (
        workspace(schedule.region.tenant_id)
        if reads_people(include, schedule.hide_recipients, explicit)
        else NO_MEMBERS
    )
    return Resolved(explicit, members, effective(include, explicit, members))


def effective_recipients(schedule: ReminderSchedule) -> list[str]:
    return resolve(schedule).addresses


@dataclass(frozen=True)
class TypedChoice:
    """`members` are the address's members when its To or Cc was chosen."""

    role: str
    members: frozenset[int] = frozenset()


NO_CHOICE = TypedChoice("")
HIDDEN = TypedChoice(RecipientRole.BCC)


@dataclass(frozen=True)
class Choices:
    _typed: Mapping[str, TypedChoice]
    _chosen: Mapping[int, RecipientRole]
    _hidden_ties: frozenset[int] = frozenset()

    def typed(self, address: str) -> TypedChoice | None:
        return self._typed.get(recipient_key(address))

    def of(self, member: Member) -> RecipientRole | None:
        return members_choice(self._chosen[pk] for pk in member.memberships if pk in self._chosen)

    def hides(self, member: Member) -> bool:
        """Whether one of the members is tied to a typed row whose choice is Hidden."""
        return not self._hidden_ties.isdisjoint(member.memberships)


TypedRow = tuple[str, TypedChoice, frozenset[int]]


def choices(typed: Iterable[TypedRow], chosen: Mapping[int, RecipientRole]) -> Choices:
    """A schedule holds one typed row per key."""
    rows = list(typed)
    return Choices(
        {recipient_key(address): choice for address, choice, _ in rows if choice.role},
        chosen,
        frozenset(
            pk for _, choice, ties in rows if choice.role == RecipientRole.BCC for pk in ties
        ),
    )


def typed_choices(schedule: ReminderSchedule) -> list[TypedRow]:
    """Each typed row's address, choice and ties, in one statement."""
    found: dict[int, tuple[str, TypedChoice, set[int]]] = {}
    for pk, address, role, members, tie in (
        ReminderRecipient.objects.filter(schedule_id=schedule.pk)
        .order_by("pk")
        .values_list("pk", "address", "role", "role_members", "ties__membership_id")
    ):
        row = found.setdefault(pk, (address, TypedChoice(role, frozenset(members)), set()))
        if tie is not None:
            row[2].add(tie)
    return [(address, choice, frozenset(ties)) for address, choice, ties in found.values()]


def stored_choices(schedule: ReminderSchedule) -> Choices:
    chosen = ReminderMemberRole.objects.filter(schedule_id=schedule.pk)
    return choices(
        typed_choices(schedule),
        {pk: RecipientRole(role) for pk, role in chosen.values_list("membership_id", "role")},
    )


def candidates(addresses: Sequence[str], members: Workspace, choices: Choices) -> list[Candidate]:
    found = []
    for address in addresses:
        member, typed = members.member(address), choices.typed(address)
        found.append(
            Candidate(
                address,
                member=member is not None,
                administrator=member is not None and member.administrator,
                chosen=choices.of(member) if member else None,
                typed=RecipientRole(typed.role) if typed else None,
                counts=typed is None or members.are_members(address, typed.members),
                tied_hidden=member is not None and choices.hides(member),
            )
        )
    return found


def placements(
    schedule: ReminderSchedule,
    members: Workspace,
    addresses: Sequence[str],
    read: Choices | None = None,
) -> list[RecipientRole]:
    """The header of each address of the list; a schedule sent hidden, or a read of people
    holding an address that is not plain, reads no choice and hides every address."""
    hidden = sent_hidden(schedule.hide_recipients) or members.holds_non_plain
    found = (
        [Candidate(address) for address in addresses]
        if hidden
        else candidates(addresses, members, stored_choices(schedule) if read is None else read)
    )
    return roles(
        found,
        hide_recipients=hidden,
        include_workspace_members=schedule.include_workspace_members,
    )


def headers(
    addresses: Sequence[str], placed: Sequence[RecipientRole]
) -> tuple[list[str], list[str], list[str]]:
    """`To`, `Cc` and `Bcc`, each key once in its address's text at its first placement."""
    found: dict[RecipientRole, list[str]] = {role: [] for role in RecipientRole}
    keys: set[str] = set()
    for address, role in zip(addresses, placed, strict=True):
        if (key := recipient_key(address)) not in keys:
            keys.add(key)
            found[role].append(address)
    return found[RecipientRole.TO], found[RecipientRole.CC], found[RecipientRole.BCC]


def planned_recipients(
    members: Workspace, stored: Sequence[Explicit], addresses: Sequence[str]
) -> list[Explicit]:
    """Each address keeps its ties and gains those of the members who hold it now, so an email
    change of its member never makes it an external address."""
    ties: dict[str, frozenset[int]] = defaultdict(frozenset)
    for row in stored:
        ties[recipient_key(row.address)] |= row.ties
    return [
        Explicit(address, ties.get(recipient_key(address), frozenset()) | members.holders(address))
        for address in addresses
    ]


def store_recipients(
    schedule: ReminderSchedule,
    stored: Sequence[Explicit],
    planned: Sequence[Explicit],
    *,
    roles: Sequence[TypedChoice],
    stored_roles: Sequence[TypedChoice],
) -> bool:
    """Rewrites the rows when they, their ties or their aligned choices differ; whether it did."""
    if list(stored) == list(planned) and list(roles) == list(stored_roles):
        return False
    ReminderRecipient.objects.filter(schedule=schedule).delete()
    rows = ReminderRecipient.objects.bulk_create(
        ReminderRecipient(
            schedule=schedule,
            address=row.address,
            key=recipient_key(row.address),
            role=choice.role,
            role_members=sorted(choice.members),
        )
        for row, choice in zip(planned, roles, strict=True)
    )
    ReminderRecipientTie.objects.bulk_create(
        ReminderRecipientTie(recipient=stored_row, membership_id=membership_id)
        for stored_row, row in zip(rows, planned, strict=True)
        for membership_id in sorted(row.ties)
    )
    return True


def store_member_choices(
    schedule: ReminderSchedule, choices: Mapping[int, RecipientRole | None]
) -> bool:
    """Stores or, for `None`, deletes each named membership's choice; whether any changed."""
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
    """Writes every remaining row's header of this attempt, so a retry keeps no earlier role."""
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


def _envelope(delivery: NotificationDelivery) -> list[DeliveryRecipient]:
    return [
        row
        for row in delivery.recipient_rows.all()
        if delivery.transmit_started_at is not None or not row.revoked
    ]


def delivery_addresses(delivery: NotificationDelivery) -> list[str]:
    """The envelope once the transmit marker is set, otherwise the rows not revoked."""
    return [row.address for row in _envelope(delivery)]


def delivery_roles(delivery: NotificationDelivery) -> list[tuple[str, RecipientRole]] | None:
    """Only for a message that reached or may have reached anyone: a rejected one showed none."""
    if delivery.status not in (
        NotificationDeliveryStatus.SENT,
        NotificationDeliveryStatus.UNCERTAIN,
    ):
        return None
    return [(row.address, RecipientRole(row.role)) for row in _envelope(delivery)]


def hide_visible_schedules(
    tenant_ids: Collection[int], schedule_ids: Collection[int] = ()
) -> set[int]:
    """Hides every visible schedule of the workspaces and of the ids until an administrator
    saves again; the schedules it changed."""
    hidden = set(
        ReminderSchedule.objects.select_for_update(of=("self",))
        .filter(Q(region__tenant_id__in=tenant_ids) | Q(pk__in=schedule_ids), hide_recipients=False)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    ReminderSchedule.objects.filter(pk__in=hidden).update(
        hide_recipients=True, hidden_by_departure=True, revision=F("revision") + 1
    )
    return hidden


def remove_member_recipients(tenant_id: int, membership_id: int, emails: set[str]) -> None:
    list(
        Region.objects.select_for_update()
        .filter(tenant_id=tenant_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    hidden = hide_visible_schedules([tenant_id])
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
    ReminderSchedule.objects.filter(pk__in=changed - hidden).update(revision=F("revision") + 1)
    _revoke_unauthorized(tenant_id, membership_id, keys)


def _revoke_unauthorized(tenant_id: int, leaving: int, keys: set[str]) -> None:
    """Revokes the open deliveries' rows of these keys that their schedule no longer authorizes
    without the leaving membership; an address another active member holds stays."""
    if not keys:
        return
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
