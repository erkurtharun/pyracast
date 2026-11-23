from collections.abc import Sequence

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import QuerySet, Value
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.authorization import require_platform_admin
from pyracast.connections.identity import canonical_account_identity
from pyracast.connections.models import MAX_DAILY_QUOTA, Provider, ProviderAccount
from pyracast.connections.secrets import api_key_fingerprints, encrypt_secret
from pyracast.errors import ConflictError, ErrorCode
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction


def _refuse_shared_key(
    fingerprints: Sequence[str], *, other_than: QuerySet[ProviderAccount]
) -> None:
    holders = ProviderAccount.objects.filter(api_key_fingerprint__in=fingerprints).exclude(
        pk__in=other_than
    )
    holder = holders.values_list("external_account_id", flat=True).first()
    if holder is not None:
        raise ValidationError(
            {
                "api_key": ValidationError(
                    gettext(
                        "This API key is already stored on the account %(account)s. "
                        "Use that account instead."
                    )
                    % {"account": holder},
                    code="shared_api_key",
                )
            }
        )


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
    if provider == Provider.FIXTURE and not settings.PYRACAST_FIXTURE_PROVIDER:
        raise ValidationError(gettext("Fixture accounts are available only in development."))
    if provider == Provider.SOLCAST and not api_key:
        raise ValidationError({"api_key": gettext("An API key is required for a Solcast account.")})
    encrypted_api_key = encrypt_secret(api_key) if api_key else ""
    fingerprints = api_key_fingerprints(api_key) if api_key else ()
    if not 1 <= daily_quota <= MAX_DAILY_QUOTA:
        raise ValidationError(gettext("Account quota must be between 1 and 10."))
    authorization_reference = authorization_reference.strip()
    if (provider == Provider.SOLCAST and not authorization_reference) or (
        len(authorization_reference) > 500
    ):
        raise ValidationError(gettext("A service authorization reference is required."))
    accounts = ProviderAccount.objects.alias(
        canonical_provider=canonical_account_identity("provider"),
        canonical_identity=canonical_account_identity("external_account_id"),
    ).filter(
        canonical_provider__exact=canonical_account_identity(Value(provider)),
        canonical_identity__exact=canonical_account_identity(Value(identity)),
    )
    with transaction.atomic():
        existing: ProviderAccount | None = accounts.first()
        if existing is None:
            _refuse_shared_key(fingerprints, other_than=accounts)
            try:
                with transaction.atomic():
                    created = ProviderAccount.objects.create(
                        provider=provider,
                        external_account_id=canonical_account_identity(Value(identity)),
                        encrypted_api_key=encrypted_api_key,
                        api_key_fingerprint=fingerprints[0] if fingerprints else None,
                        daily_quota=daily_quota,
                        authorization_reference=authorization_reference
                        or "fixture-development-only",
                        authorized_by=actor,
                        authorized_at=timezone.now(),
                    )
            except IntegrityError:
                existing = accounts.first()
                if existing is None:
                    _refuse_shared_key(fingerprints, other_than=accounts)
                    raise
            else:
                created.refresh_from_db(fields=("provider", "external_account_id"))
                record_audit(
                    actor=actor,
                    provider_account=created,
                    action=AuditAction.PROVIDER_ACCOUNT_CREATED,
                    revision=created.credential_revision,
                )
                return created
        if require_new:
            raise ConflictError(
                ErrorCode.CONFLICT,
                gettext("This account already exists. Select the existing account."),
            )
    return existing


@sensitive_variables()
def replace_api_key(actor: User, account_id: int, api_key: str, *, expected_revision: int) -> None:
    """Replaces the key the page showed at `expected_revision`; every approved plant of the
    account is then revalidated in the background."""
    require_platform_admin(actor)
    encrypted_api_key = encrypt_secret(api_key)
    fingerprints = api_key_fingerprints(api_key)
    with transaction.atomic():
        this_account = ProviderAccount.objects.filter(pk=account_id)
        account = this_account.select_for_update().get()
        if account.credential_revision != expected_revision:
            raise ConflictError(
                ErrorCode.CONFLICT, gettext("The account changed. Reload this page.")
            )
        _refuse_shared_key(fingerprints, other_than=this_account)
        account.encrypted_api_key = encrypted_api_key
        account.api_key_fingerprint = fingerprints[0]
        account.credential_revision += 1
        account.revalidation_due_at = timezone.now()
        try:
            with transaction.atomic():
                account.save(
                    update_fields=(
                        "encrypted_api_key",
                        "api_key_fingerprint",
                        "credential_revision",
                        "revalidation_due_at",
                    )
                )
        except IntegrityError:
            _refuse_shared_key(fingerprints, other_than=this_account)
            raise
        record_audit(
            actor=actor,
            provider_account=account,
            action=AuditAction.PROVIDER_SECRET_ROTATED,
            revision=account.credential_revision,
        )
