from dataclasses import dataclass, replace
from datetime import datetime

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connection, transaction
from django.utils import translation
from django.utils.translation import gettext

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import Language, WorkspaceRole
from pyracast.connections.models import MAX_DAILY_QUOTA
from pyracast.errors import ConflictError, DomainError, ErrorCode
from pyracast.local_time import CLOCK_PATTERN
from pyracast.notifications.eligibility import binding_can_notify
from pyracast.notifications.models import (
    NotificationSettings,
    ReminderSchedule,
)
from pyracast.notifications.recipients import (
    NO_MEMBERS,
    effective,
    explicit_recipients,
    grouped_recipients,
    needs_members,
    planned_recipients,
    store_recipients,
    workspace,
)
from pyracast.notifications.recipients import effective_recipients as effective_recipients
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, Region, RegionStatus, active_region

RECIPIENT_MAX_LENGTH = 254
# The largest per-plant recipient limit the console accepts (a smallint column).
MAX_RECIPIENT_LIMIT = 32767


def validate_recipient(address: str) -> None:
    if len(address) > RECIPIENT_MAX_LENGTH:
        raise ValidationError(
            gettext("Recipient addresses cannot exceed %(limit)s characters."),
            params={"limit": RECIPIENT_MAX_LENGTH},
        )
    validate_email(address)


def invalid_reminder_recipient_count() -> int:
    schedules: dict[int, tuple[bool, int]] = {}
    rows: list[tuple[int, int, str, int | None]] = []
    for pk, include, tenant_id, row, address, membership_id in (
        ReminderSchedule.objects.filter(
            active_region("region__"),
            enabled=True,
            region__status=RegionStatus.APPROVED,
            region__approved_binding__provider_account__enabled=True,
        )
        .order_by("pk", "recipient_rows__pk")
        .values_list(
            "pk",
            "include_workspace_members",
            "region__tenant_id",
            "recipient_rows__pk",
            "recipient_rows__address",
            "recipient_rows__ties__membership_id",
        )
    ):
        schedules[pk] = (include, tenant_id)
        if row is not None:
            rows.append((pk, row, address, membership_id))
    explicit = grouped_recipients(rows)
    tenants = {
        tenant_id
        for pk, (include, tenant_id) in schedules.items()
        if needs_members(include, explicit.get(pk, []))
    }
    members = {tenant_id: workspace(tenant_id) for tenant_id in tenants}
    invalid = 0
    for pk, (include, tenant_id) in schedules.items():
        current = members.get(tenant_id, NO_MEMBERS)
        try:
            for address in effective(include, explicit.get(pk, []), current):
                validate_recipient(address)
        except ValidationError:
            invalid += 1
    return invalid


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
        region=region, effective_from=now, language=translation.get_language()
    )
    return schedule, membership.role == WorkspaceRole.ADMIN


def _check_recipients(submitted: ReminderSettings) -> None:
    for address in submitted.recipients:
        validate_recipient(address)
    if len({address.casefold() for address in submitted.recipients}) != len(submitted.recipients):
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
        current = len(effective(stored.include_workspace_members, explicit, members))
        planned = planned_recipients(tenant_id, explicit, submitted.recipients)
        schedule.include_workspace_members = include_workspace_members
        unchanged_pause = not enabled and replace(submitted, enabled=stored.enabled) == stored
        _check_recipient_limit(
            len(effective(include_workspace_members, planned, members)),
            current,
            limit,
            unchanged_pause,
        )
        store_recipients(schedule, explicit, planned)
        schedule.enabled, schedule.times, schedule.language = (
            enabled,
            list(submitted.times),
            language,
        )
        if created or submitted != stored:
            schedule.configured_by = actor
            schedule.effective_from = now
            schedule.revision += 1
            schedule.save()
            record_audit(
                AuditAction.REMINDER_SCHEDULE_UPDATED,
                actor=actor,
                region=region,
                revision=region.revision,
                details={"schedule_revision": schedule.revision},
            )
        return schedule
