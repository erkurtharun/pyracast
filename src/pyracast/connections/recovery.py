from datetime import datetime

from django.contrib.auth.models import User
from django.db import transaction
from django.utils.translation import gettext

from pyracast.accounts.authorization import require_platform_admin
from pyracast.connections.models import ProviderAccount
from pyracast.errors import ConflictError, ErrorCode
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction


def clear_provider_cooldown(
    actor: User,
    account_id: int,
    credential_revision: int,
    *,
    expected_blocked_until: datetime | None,
) -> bool:
    require_platform_admin(actor)
    with transaction.atomic():
        account = ProviderAccount.objects.select_for_update().get(pk=account_id)
        if account.credential_revision != credential_revision:
            raise ConflictError(
                ErrorCode.CONFLICT,
                gettext("The account changed. Reload before confirming recovery."),
            )
        if account.request_blocked_until is None:
            return False
        if account.request_blocked_until != expected_blocked_until:
            raise ConflictError(
                ErrorCode.CONFLICT,
                gettext("The provider delay changed. Reload before confirming recovery."),
            )
        account.request_blocked_until = None
        account.save(update_fields=("request_blocked_until",))
        record_audit(
            actor=actor,
            provider_account=account,
            action=AuditAction.PROVIDER_COOLDOWN_CLEARED,
            revision=account.credential_revision,
        )
    return True
