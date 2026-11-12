import re
from datetime import datetime

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from pyracast.accounts.models import Membership
from pyracast.accounts.operations import require_member
from pyracast.forecasts.jobs import binding_is_current
from pyracast.notifications.models import NotificationSettings, ReminderPreference
from pyracast.regions.models import Region


def recipient_limit(*, lock: bool = False) -> int:
    if lock:
        settings, _ = NotificationSettings.objects.select_for_update().get_or_create(pk=1)
        return settings.max_recipients
    return NotificationSettings.objects.values_list("max_recipients", flat=True).first() or 5


def effective_recipients(preference: ReminderPreference) -> list[str]:
    return list(preference.recipients) or [preference.email or preference.user.email]


def normalize_recipients(recipients: list[str]) -> list[str]:
    normalized = [address.strip() for address in recipients]
    for address in normalized:
        validate_email(address)
    if len({address.casefold() for address in normalized}) != len(normalized):
        raise ValidationError("Recipient addresses must be distinct (case-insensitive).")
    return normalized


def region_recipient_count(
    region: Region, *, replacement: tuple[int, list[str]] | None = None
) -> int:
    members = Membership.objects.filter(tenant_id=region.tenant_id).values("user_id")
    preferences = ReminderPreference.objects.filter(
        region=region, user_id__in=members, user__is_active=True
    ).select_related("user")
    addresses: set[str] = set()
    for preference in preferences:
        if (
            not preference.enabled
            and not preference.times
            and not preference.recipients
            and not preference.email
        ):
            continue
        if replacement is None or preference.user_id != replacement[0]:
            addresses.update(
                address.casefold() for address in effective_recipients(preference) if address
            )
    if replacement is not None:
        addresses.update(address.casefold() for address in replacement[1] if address)
    return len(addresses)


def get_preference(
    actor: User, tenant_id: int, region_id: int, now: datetime
) -> ReminderPreference:
    require_member(actor, tenant_id)
    region = Region.objects.get(pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True)
    return ReminderPreference.objects.filter(
        user=actor, region=region
    ).first() or ReminderPreference(user=actor, region=region, effective_from=now)


def configure_reminders(
    actor: User,
    tenant_id: int,
    region_id: int,
    *,
    enabled: bool,
    email: str | None = None,
    recipients: list[str] | None = None,
    times: list[str],
    now: datetime,
) -> ReminderPreference:
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    require_member(actor, tenant_id)
    if email is not None and recipients is not None:
        raise ValidationError("Provide recipients or the legacy email field, not both.")
    addresses = normalize_recipients(
        recipients if recipients is not None else [email] if email else []
    )
    with transaction.atomic():
        address_limit = recipient_limit(lock=True)
        actor.refresh_from_db(fields=["email"])
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        preference, _ = ReminderPreference.objects.get_or_create(
            user=actor, region=region, defaults={"effective_from": now}
        )
        reserved = addresses or ([actor.email] if enabled or times else [])
        current_count = region_recipient_count(region)
        proposed_count = region_recipient_count(region, replacement=(actor.pk, reserved))
        disabling_unchanged = (
            not enabled
            and addresses
            == (preference.recipients or ([preference.email] if preference.email else []))
            and sorted(times) == preference.times
        )
        if proposed_count > address_limit and not (
            proposed_count < current_count or disabling_unchanged
        ):
            raise ValidationError(
                f"This plant allows at most {address_limit} distinct recipient addresses in total."
            )
        binding = region.approved_binding
        limit = binding.provider_account.daily_quota if binding is not None else 10
        if len(times) != len(set(times)) or len(times) > limit:
            raise ValidationError(f"Provide at most {limit} distinct local HH:MM times.")
        for clock in times:
            if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock) is None:
                raise ValidationError("Reminder times must use HH:MM.")
        if enabled:
            if not addresses:
                validate_email(actor.email)
            if not times or binding is None or not binding_is_current(binding):
                raise ValidationError(
                    "An approved current region and at least one time are required."
                )
        preference.enabled = enabled
        preference.recipients = addresses
        preference.email = addresses[0] if len(addresses) == 1 else ""
        preference.times = sorted(times)
        preference.effective_from = now
        preference.save()
        return preference
