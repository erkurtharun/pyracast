from datetime import datetime

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction

from pyracast.accounts.authorization import require_platform_admin
from pyracast.connections.models import ProviderAccount
from pyracast.regions.models import AuditEvent


def clear_provider_cooldown(
    actor: User,
    account_id: int,
    credential_revision: int,
    *,
    expected_blocked_until: datetime | None,
) -> None:
    require_platform_admin(actor)
    with transaction.atomic():
        account = ProviderAccount.objects.select_for_update().get(pk=account_id)
        if account.credential_revision != credential_revision:
            raise ValidationError("The account changed. Reload before confirming recovery.")
        if account.request_blocked_until is None:
            return
        if account.request_blocked_until != expected_blocked_until:
            raise ValidationError("The provider delay changed. Reload before confirming recovery.")
        account.request_blocked_until = None
        account.save(update_fields=("request_blocked_until",))
        AuditEvent.objects.create(
            actor=actor,
            provider_account=account,
            action="provider_cooldown_cleared",
            revision=account.credential_revision,
        )
