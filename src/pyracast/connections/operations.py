from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Value
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.authorization import require_platform_admin
from pyracast.connections.identity import canonical_account_identity
from pyracast.connections.models import MAX_DAILY_QUOTA, Provider, ProviderAccount
from pyracast.connections.secrets import encrypt_secret
from pyracast.errors import ConflictError, ErrorCode
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction


@sensitive_variables()
def create_account(
    actor: User,
    *,
    provider: str,
    external_account_id: str,
    api_key: str = "",
    daily_quota: int = MAX_DAILY_QUOTA,
    authorization_reference: str = "",
    require_new: bool = False,
) -> ProviderAccount:
    require_platform_admin(actor)
    provider = provider.strip().casefold()
    identity = external_account_id.strip()
    if provider not in Provider.values or not identity:
        raise ValidationError(gettext("Invalid provider account identity."))
    if provider == Provider.SOLCAST and not api_key:
        raise ValidationError({"api_key": gettext("An API key is required for a Solcast account.")})
    encrypted_api_key = encrypt_secret(api_key) if api_key else ""
    if not 1 <= daily_quota <= MAX_DAILY_QUOTA:
        raise ValidationError(gettext("Account quota must be between 1 and 10."))
    authorization_reference = authorization_reference.strip()
    if (provider == Provider.SOLCAST and not authorization_reference) or (
        len(authorization_reference) > 500
    ):
        raise ValidationError(gettext("A service authorization reference is required."))
    with transaction.atomic():
        account, created = ProviderAccount.objects.alias(
            canonical_provider=canonical_account_identity("provider"),
            canonical_identity=canonical_account_identity("external_account_id"),
        ).get_or_create(
            canonical_provider__exact=canonical_account_identity(Value(provider)),
            canonical_identity__exact=canonical_account_identity(Value(identity)),
            defaults={
                "provider": provider,
                "external_account_id": canonical_account_identity(Value(identity)),
                "encrypted_api_key": encrypted_api_key,
                "daily_quota": daily_quota,
                "authorization_reference": authorization_reference or "fixture-development-only",
                "authorized_by": actor,
                "authorized_at": timezone.now(),
            },
        )
        if not created and require_new:
            raise ConflictError(
                ErrorCode.CONFLICT,
                gettext("This account already exists. Select the existing account."),
            )
        if created:
            account.refresh_from_db(fields=("provider", "external_account_id"))
            record_audit(
                actor=actor,
                provider_account=account,
                action=AuditAction.PROVIDER_ACCOUNT_CREATED,
                revision=account.credential_revision,
            )
    return account


@sensitive_variables()
def rotate_secret(actor: User, account_id: int, api_key: str) -> None:
    require_platform_admin(actor)
    encrypted_api_key = encrypt_secret(api_key)
    with transaction.atomic():
        account = ProviderAccount.objects.select_for_update().get(pk=account_id)
        account.encrypted_api_key = encrypted_api_key
        account.credential_revision += 1
        account.save(update_fields=("encrypted_api_key", "credential_revision"))
        record_audit(
            actor=actor,
            provider_account=account,
            action=AuditAction.PROVIDER_SECRET_ROTATED,
            revision=account.credential_revision,
        )
