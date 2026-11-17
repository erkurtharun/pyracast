import re
from datetime import datetime

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import translation
from django.utils.translation import gettext

from pyracast.accounts.operations import require_member, require_workspace_admin
from pyracast.notifications.eligibility import binding_can_notify
from pyracast.notifications.models import (
    NotificationSettings,
    ReminderSchedule,
)
from pyracast.notifications.recipients import effective_recipients as effective_recipients
from pyracast.notifications.recipients import recipient_memberships
from pyracast.regions.models import Region

RECIPIENT_MAX_LENGTH = 254


def validate_recipient(address: str) -> None:
    if len(address) > RECIPIENT_MAX_LENGTH:
        raise ValidationError(
            gettext("Recipient addresses cannot exceed %(limit)s characters."),
            params={"limit": RECIPIENT_MAX_LENGTH},
        )
    validate_email(address)


def invalid_reminder_recipient_count() -> int:
    invalid = 0
    for schedule in ReminderSchedule.objects.filter(
        enabled=True,
        region__deleted_at__isnull=True,
        region__status="approved",
        region__approved_binding__provider_account__enabled=True,
    ).select_related("region"):
        try:
            for address in effective_recipients(schedule):
                validate_recipient(address)
        except ValidationError, TypeError:
            invalid += 1
    return invalid


def recipient_limit(*, lock: bool = False) -> int:
    if lock:
        settings, _ = NotificationSettings.objects.select_for_update().get_or_create(pk=1)
        return settings.max_recipients
    return NotificationSettings.objects.values_list("max_recipients", flat=True).first() or 5


def normalize_recipients(recipients: list[str]) -> list[str]:
    normalized = [address.strip() for address in recipients]
    for address in normalized:
        validate_recipient(address)
    if len({address.casefold() for address in normalized}) != len(normalized):
        raise ValidationError(gettext("Recipient addresses must be distinct (case-insensitive)."))
    return normalized


def _reminder_settings(schedule: ReminderSchedule) -> tuple[object, ...]:
    return (
        schedule.enabled,
        list(schedule.recipients),
        schedule.include_workspace_members,
        list(schedule.times),
        schedule.language,
    )


def get_reminder_schedule(
    actor: User, tenant_id: int, region_id: int, now: datetime
) -> ReminderSchedule:
    require_member(actor, tenant_id)
    region = Region.objects.get(pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True)
    return ReminderSchedule.objects.filter(region=region).first() or ReminderSchedule(
        region=region, effective_from=now, language=translation.get_language()
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
    now: datetime,
    include_workspace_members: bool = False,
) -> ReminderSchedule:
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    if language not in {"en", "tr"}:
        raise ValidationError(gettext("A supported reminder language is required."))
    with transaction.atomic():
        require_workspace_admin(actor, tenant_id)
        address_limit = recipient_limit(lock=True)
        actor.refresh_from_db(fields=["email"])
        addresses = normalize_recipients(
            recipients if recipients or include_workspace_members else [actor.email]
        )
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        schedule, created = ReminderSchedule.objects.get_or_create(
            region=region, defaults={"effective_from": now}
        )
        stored = _reminder_settings(schedule)
        stored_memberships = dict(schedule.recipient_memberships)
        current_count = len(effective_recipients(schedule))
        disabling_unchanged = (
            not enabled
            and addresses == schedule.recipients
            and sorted(times) == schedule.times
            and include_workspace_members == schedule.include_workspace_members
        )
        schedule.recipients = addresses
        schedule.include_workspace_members = include_workspace_members
        known_members = recipient_memberships(tenant_id)
        known_members.update(schedule.recipient_memberships)
        schedule.recipient_memberships = {
            address.casefold(): known_members[address.casefold()]
            for address in addresses
            if address.casefold() in known_members
        }
        proposed_count = len(effective_recipients(schedule))
        if proposed_count > address_limit and not (
            proposed_count < current_count or disabling_unchanged
        ):
            raise ValidationError(
                gettext(
                    "This plant allows at most %(limit)s distinct recipient addresses in total."
                ),
                params={"limit": address_limit},
            )
        binding = region.approved_binding
        limit = binding.provider_account.daily_quota if binding is not None else 10
        if len(times) != len(set(times)) or len(times) > limit:
            raise ValidationError(
                gettext("Provide at most %(limit)s distinct local HH:MM times."),
                params={"limit": limit},
            )
        for clock in times:
            if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock) is None:
                raise ValidationError(gettext("Reminder times must use HH:MM."))
        if enabled and (not times or binding is None or not binding_can_notify(binding)):
            raise ValidationError(
                gettext("An approved current region and at least one time are required.")
            )
        schedule.enabled = enabled
        schedule.recipients = addresses
        schedule.times = sorted(times)
        schedule.language = language
        if not created and _reminder_settings(schedule) == stored:
            if schedule.recipient_memberships != stored_memberships:
                schedule.save(update_fields=["recipient_memberships"])
            return schedule
        schedule.configured_by = actor
        schedule.effective_from = now
        schedule.save()
        return schedule
