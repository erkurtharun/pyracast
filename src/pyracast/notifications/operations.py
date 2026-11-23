from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import translation
from django.utils.translation import gettext

from pyracast.accounts.authorization import require_platform_admin, tenant_membership
from pyracast.accounts.models import Language, WorkspaceRole
from pyracast.connections.models import MAX_DAILY_QUOTA
from pyracast.errors import ConflictError, DomainError, ErrorCode
from pyracast.local_time import CLOCK_PATTERN
from pyracast.notifications.eligibility import binding_can_notify
from pyracast.notifications.models import (
    NotificationSettings,
    RecipientRole,
    ReminderSchedule,
)
from pyracast.notifications.recipients import (
    HIDDEN,
    NO_CHOICE,
    NO_MEMBERS,
    Choices,
    Explicit,
    Resolved,
    TypedChoice,
    Workspace,
    addressable,
    choices,
    effective,
    explicit_recipients,
    grouped_recipients,
    mailbox_key,
    needs_members,
    placements,
    planned_recipients,
    store_member_choices,
    store_recipients,
    typed_choices,
    workspace,
    workspaces,
)
from pyracast.notifications.recipients import effective_recipients as effective_recipients
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, Region, RegionStatus, active_region

# The largest per-plant recipient limit the console accepts (a smallint column).
MAX_RECIPIENT_LIMIT = 32767


def validate_recipient(address: str) -> None:
    addressable(address)


def _all_valid(addresses: list[str]) -> bool:
    try:
        for address in addresses:
            validate_recipient(address)
    except ValidationError:
        return False
    return True


def invalid_recipient_region_ids() -> set[int]:
    """Regions whose enabled reminders would go to an invalid address, in at most three
    statements."""
    schedules: dict[int, tuple[int, bool, int]] = {}
    rows: list[tuple[int, int, str, int | None]] = []
    for pk, region_id, include, tenant_id, row, address, membership_id in (
        ReminderSchedule.objects.filter(
            active_region("region__"),
            enabled=True,
            region__status=RegionStatus.APPROVED,
            region__approved_binding__provider_account__enabled=True,
        )
        .order_by("pk", "recipient_rows__pk")
        .values_list(
            "pk",
            "region_id",
            "include_workspace_members",
            "region__tenant_id",
            "recipient_rows__pk",
            "recipient_rows__address",
            "recipient_rows__ties__membership_id",
        )
    ):
        schedules[pk] = (region_id, include, tenant_id)
        if row is not None:
            rows.append((pk, row, address, membership_id))
    explicit = grouped_recipients(rows)
    members = workspaces(
        {
            tenant_id
            for pk, (_, include, tenant_id) in schedules.items()
            if needs_members(include, explicit.get(pk, []))
        }
    )
    return {
        region_id
        for pk, (region_id, include, tenant_id) in schedules.items()
        if not _all_valid(
            effective(include, explicit.get(pk, []), members.get(tenant_id, NO_MEMBERS))
        )
    }


SHARED_LIMIT = (
    "SELECT max_recipients FROM notifications_notificationsettings WHERE id = 1 FOR SHARE"
)


def recipient_limit(*, lock: bool = False) -> int:
    """`lock` holds a share lock until commit: readers never wait for each other, and the
    platform form's change of the limit waits for every check made under the old one."""
    if not lock:
        stored = NotificationSettings.objects.values_list("max_recipients", flat=True).first()
        return NotificationSettings().max_recipients if stored is None else stored
    with connection.cursor() as cursor:
        cursor.execute(SHARED_LIMIT)
        row = cursor.fetchone()
    if row is None:
        NotificationSettings.objects.get_or_create(pk=1)
        return recipient_limit(lock=True)
    return int(row[0])


@dataclass(frozen=True)
class RecipientLimitChange:
    previous: int
    current: int

    @property
    def changed(self) -> bool:
        return self.previous != self.current


def set_recipient_limit(actor: User, value: int, *, expected: int) -> RecipientLimitChange:
    """A value equal to the stored limit is reported unchanged before the stale check, so a
    repeated submit is not refused."""
    require_platform_admin(actor)
    with transaction.atomic():
        settings, _created = NotificationSettings.objects.select_for_update().get_or_create(pk=1)
        previous = settings.max_recipients
        if value == previous:
            return RecipientLimitChange(previous, previous)
        if previous != expected:
            raise ConflictError(
                ErrorCode.CONFLICT, gettext("The settings changed. Reload this page before saving.")
            )
        settings.max_recipients = value
        settings.full_clean()
        settings.save(update_fields=("max_recipients",))
        LogEntry.objects.create(
            user=actor,
            content_type=ContentType.objects.get_for_model(NotificationSettings),
            object_id=str(settings.pk),
            object_repr="Email reminder settings",
            action_flag=CHANGE,
            change_message=f"Recipient limit changed from {previous} to {value}.",
        )
    return RecipientLimitChange(previous, value)


@dataclass(frozen=True)
class AddressChoice:
    """`memberships` are the members of the address the form showed; `None` clears the choice."""

    address: str
    role: RecipientRole | None
    memberships: frozenset[int]


@dataclass(frozen=True)
class MemberChoice:
    """`memberships` and `address` are a member row the form showed; `None` deletes the choice."""

    memberships: frozenset[int]
    address: str
    role: RecipientRole | None


@dataclass(frozen=True)
class ReminderSettings:
    enabled: bool
    recipients: tuple[str, ...]
    include_workspace_members: bool
    times: tuple[str, ...]
    language: str


def get_reminder_schedule(
    actor: User, tenant_id: int, region_id: int, now: datetime
) -> tuple[ReminderSchedule, bool]:
    """The plant's schedule, or an unsaved default, and whether the actor administers it."""
    membership = tenant_membership(actor, tenant_id)
    region = Region.objects.active().get(pk=region_id, tenant_id=tenant_id)
    schedule = ReminderSchedule.objects.filter(region=region).first() or ReminderSchedule(
        region=region,
        effective_from=now,
        language=translation.get_language(),
        hide_recipients=False,
    )
    return schedule, membership.role == WorkspaceRole.ADMIN


def _check_recipients(submitted: ReminderSettings) -> None:
    for address in submitted.recipients:
        try:
            validate_recipient(address)
        except ValidationError as error:
            raise ValidationError({"recipients": error}) from None
    if len({mailbox_key(address) for address in submitted.recipients}) != len(submitted.recipients):
        raise DomainError(ErrorCode.DUPLICATE_RECIPIENTS, field="recipients")
    if submitted.enabled and not (submitted.recipients or submitted.include_workspace_members):
        raise DomainError(ErrorCode.RECIPIENT_REQUIRED, field="recipients")


def _check_times(submitted: ReminderSettings, stored: ReminderSettings, region: Region) -> None:
    if any(CLOCK_PATTERN.fullmatch(clock) is None for clock in submitted.times):
        raise ValidationError(gettext("Reminder times must use HH:MM."))
    if len(set(submitted.times)) != len(submitted.times):
        raise DomainError(ErrorCode.DUPLICATE_REMINDER_TIMES, field="times")
    if submitted.enabled and not submitted.times:
        raise DomainError(ErrorCode.REMINDER_TIME_REQUIRED, field="times")
    binding = region.approved_binding
    limit = binding.provider_account.daily_quota if binding is not None else MAX_DAILY_QUOTA
    # Times kept from before a quota reduction stay saved; only a longer list is refused.
    if len(submitted.times) > limit and len(submitted.times) > len(stored.times):
        raise DomainError(
            ErrorCode.TOO_MANY_REMINDER_TIMES,
            gettext("This plant allows at most %(limit)s delivery times a day.") % {"limit": limit},
            field="times",
        )


def _check_enabling(region: Region) -> None:
    binding = region.approved_binding
    if region.status != RegionStatus.APPROVED or binding is None:
        raise DomainError(ErrorCode.PLANT_NOT_APPROVED, field="enabled")
    account = binding.provider_account
    if not account.enabled or account.authorized_at is None:
        raise DomainError(ErrorCode.PROVIDER_ACCOUNT_UNAVAILABLE, field="enabled")
    if not binding_can_notify(binding):
        raise DomainError(ErrorCode.PLANT_NOT_APPROVED, field="enabled")


def _check_recipient_limit(count: int, current: int, limit: int, unchanged_pause: bool) -> None:
    # A save that lowers an over-limit count, or only pauses the schedule, stays possible.
    if count > limit and not (count < current or unchanged_pause):
        raise DomainError(
            ErrorCode.RECIPIENT_LIMIT_EXCEEDED,
            gettext("This plant allows at most %(limit)s distinct recipient addresses in total.")
            % {"limit": limit},
            field="recipients",
        )


def _check_entries(entries: Iterable[AddressChoice | MemberChoice], members: Workspace) -> None:
    """Every entry names exactly the members of its address in this read of people."""
    if not all(members.are_members(entry.address, entry.memberships) for entry in entries):
        raise ConflictError(ErrorCode.REMINDER_SCHEDULE_CHANGED)


def _member_choices(choices: Sequence[MemberChoice]) -> dict[int, RecipientRole | None]:
    decided: dict[int, RecipientRole | None] = {}
    for choice in choices:
        decided.update(dict.fromkeys(choice.memberships, choice.role))
    return decided


def _typed_choice(entry: AddressChoice) -> TypedChoice:
    if entry.role is None:
        return NO_CHOICE
    if entry.role == RecipientRole.BCC:
        return HIDDEN
    return TypedChoice(entry.role, entry.memberships)


def _typed_choices(
    planned: Sequence[Explicit], stored: Choices, changes: Sequence[AddressChoice]
) -> list[TypedChoice]:
    named = {mailbox_key(entry.address): entry for entry in changes}
    return [
        _typed_choice(entry)
        if (entry := named.get(mailbox_key(row.address)))
        else stored.typed(row.address) or NO_CHOICE
        for row in planned
    ]


def _audit_details(schedule: ReminderSchedule, resolved: Resolved) -> dict[str, int]:
    visible = [
        address
        for address, role in zip(
            resolved.addresses,
            placements(schedule, resolved.members, resolved.addresses),
            strict=True,
        )
        if role != RecipientRole.BCC
    ]
    return {
        "schedule_revision": schedule.revision,
        "visible_recipients": len(visible),
        "visible_outside": sum(resolved.members.member(address) is None for address in visible),
    }


def configure_reminders(
    actor: User,
    tenant_id: int,
    region_id: int,
    *,
    enabled: bool,
    recipients: list[str],
    times: list[str],
    language: str,
    include_workspace_members: bool = False,
    now: datetime,
    expected_revision: int | None = None,
    hide_recipients: bool = True,
    address_roles: Sequence[AddressChoice] | None = None,
    member_roles: Sequence[MemberChoice] | None = None,
) -> ReminderSchedule:
    """`expected_revision` is the revision the administrator's form was read at; a schedule
    changed since then is refused instead of overwritten."""
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    if language not in Language.values:
        raise ValidationError(gettext("A supported reminder language is required."))
    submitted = ReminderSettings(
        enabled,
        tuple(address.strip() for address in recipients),
        include_workspace_members,
        tuple(sorted(times)),
        language,
    )
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        limit = recipient_limit(lock=True)
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        schedule, created = ReminderSchedule.objects.get_or_create(
            region=region, defaults={"effective_from": now}
        )
        if expected_revision is not None and expected_revision != schedule.revision:
            raise ConflictError(ErrorCode.REMINDER_SCHEDULE_CHANGED)
        explicit = explicit_recipients([schedule.pk])[schedule.pk]
        stored = ReminderSettings(
            schedule.enabled,
            tuple(row.address for row in explicit),
            schedule.include_workspace_members,
            tuple(schedule.times),
            schedule.language,
        )
        _check_recipients(submitted)
        _check_times(submitted, stored, region)
        if enabled and (created or not stored.enabled):
            _check_enabling(region)
        members = workspace(tenant_id)
        _check_entries([*(address_roles or ()), *(member_roles or ())], members)
        decided = _member_choices(member_roles or ())
        current = len(effective(stored.include_workspace_members, explicit, members))
        planned = planned_recipients(members, explicit, submitted.recipients)
        schedule.include_workspace_members = include_workspace_members
        unchanged_pause = not enabled and replace(submitted, enabled=stored.enabled) == stored
        addresses = effective(include_workspace_members, planned, members)
        _check_recipient_limit(len(addresses), current, limit, unchanged_pause)
        stored_typed = typed_choices(schedule)
        stored_roles = [choice for _, choice, _ in stored_typed]
        roles = _typed_choices(planned, choices(stored_typed, {}), address_roles or ())
        rows = store_recipients(schedule, explicit, planned, roles=roles, stored_roles=stored_roles)
        chosen = store_member_choices(schedule, decided)
        mode = schedule.hide_recipients != hide_recipients or schedule.hidden_by_departure
        schedule.enabled, schedule.times, schedule.language, schedule.hide_recipients = (
            enabled,
            list(submitted.times),
            language,
            hide_recipients,
        )
        schedule.hidden_by_departure = False
        configured = created or submitted != stored or mode or roles != stored_roles or chosen
        if configured:
            schedule.configured_by = actor
            schedule.effective_from = now
        if configured or rows:
            schedule.revision += 1
            schedule.save()
            record_audit(
                AuditAction.REMINDER_SCHEDULE_UPDATED,
                actor=actor,
                region=region,
                revision=region.revision,
                details=_audit_details(schedule, Resolved(planned, members, addresses)),
            )
        return schedule
