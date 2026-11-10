import re
from datetime import datetime

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from pyracast.accounts.operations import require_member
from pyracast.forecasts.jobs import binding_is_current
from pyracast.notifications.models import ReminderPreference
from pyracast.regions.models import Region


def get_preference(
    actor: User, tenant_id: int, region_id: int, now: datetime
) -> ReminderPreference:
    require_member(actor, tenant_id)
    region = Region.objects.get(pk=region_id, tenant_id=tenant_id)
    return ReminderPreference.objects.filter(
        user=actor, region=region
    ).first() or ReminderPreference(user=actor, region=region, effective_from=now)


def configure_reminders(
    actor: User,
    tenant_id: int,
    region_id: int,
    *,
    enabled: bool,
    email: str,
    times: list[str],
    now: datetime,
) -> ReminderPreference:
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    require_member(actor, tenant_id)
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
        preference, _ = ReminderPreference.objects.get_or_create(
            user=actor, region=region, defaults={"effective_from": now}
        )
        binding = region.approved_binding
        limit = binding.provider_account.daily_quota if binding is not None else 10
        if len(times) != len(set(times)) or len(times) > limit:
            raise ValidationError(f"Provide at most {limit} distinct local HH:MM times.")
        for clock in times:
            if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock) is None:
                raise ValidationError("Reminder times must use HH:MM.")
        if email:
            validate_email(email)
        if enabled:
            validate_email(email or actor.email)
            if not times or binding is None or not binding_is_current(binding):
                raise ValidationError(
                    "An approved current region and at least one time are required."
                )
        preference.enabled, preference.email, preference.times = enabled, email, sorted(times)
        preference.effective_from = now
        preference.save()
        return preference
