import time
from datetime import UTC, datetime, timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Value
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.operations import require_admin
from pyracast.connections.contracts import ProviderAdapter, ProviderError
from pyracast.connections.identity import canonical_account_identity
from pyracast.connections.models import BindingHealth, Provider, ProviderAccount, SiteBinding
from pyracast.connections.secrets import encrypt_secret
from pyracast.forecasts.budget import remaining_forecast_requests
from pyracast.forecasts.models import (
    CollectionJob,
    RequestAttempt,
    RequestOutcome,
    RequestPurpose,
)
from pyracast.provider_retry import next_retry_at, rate_limit_retry_after
from pyracast.regions.models import AuditAction, AuditEvent, Region, RegionStatus


class QuotaExceededError(Exception):
    pass


class ForecastBudgetReservedError(QuotaExceededError):
    pass


class HistoryWindowClosedError(QuotaExceededError):
    pass


class RequestDeferredError(QuotaExceededError):
    def __init__(self, available_at: datetime | None) -> None:
        super().__init__("Provider requests are temporarily unavailable.")
        self.available_at = available_at


@sensitive_variables()
def create_account(
    actor: User,
    *,
    provider: str,
    external_account_id: str,
    api_key: str = "",
    daily_quota: int = 10,
    authorization_reference: str = "",
    require_new: bool = False,
) -> ProviderAccount:
    require_admin(actor)
    provider = provider.strip().casefold()
    identity = external_account_id.strip()
    if provider not in Provider.values or not identity:
        raise ValidationError("Invalid provider account identity.")
    if provider == Provider.SOLCAST and not api_key:
        raise ValidationError({"api_key": "An API key is required for a Solcast account."})
    encrypted_api_key = encrypt_secret(api_key) if api_key else ""
    if daily_quota < 1 or daily_quota > 10:
        raise ValidationError("Account quota must be between 1 and 10.")
    authorization_reference = authorization_reference.strip()
    if (provider == Provider.SOLCAST and not authorization_reference) or (
        len(authorization_reference) > 500
    ):
        raise ValidationError("A service authorization reference is required.")
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
            raise ValidationError("This account already exists. Select the existing account.")
        if created:
            account.refresh_from_db(fields=("provider", "external_account_id"))
            AuditEvent.objects.create(
                actor=actor,
                provider_account=account,
                action=AuditAction.PROVIDER_ACCOUNT_CREATED,
                revision=account.credential_revision,
            )
    return account


@sensitive_variables()
def rotate_secret(actor: User, account_id: int, api_key: str) -> None:
    require_admin(actor)
    encrypted_api_key = encrypt_secret(api_key)
    with transaction.atomic():
        account = ProviderAccount.objects.select_for_update().get(pk=account_id)
        account.encrypted_api_key = encrypted_api_key
        account.credential_revision += 1
        account.save(update_fields=("encrypted_api_key", "credential_revision"))
        AuditEvent.objects.create(
            actor=actor,
            provider_account=account,
            action=AuditAction.PROVIDER_SECRET_ROTATED,
            revision=account.credential_revision,
        )


def reserve_account_request(
    account: ProviderAccount,
    now: datetime,
    *,
    purpose: str,
    binding: SiteBinding | None = None,
    job: CollectionJob | None = None,
) -> RequestAttempt:
    started = time.monotonic()
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    if binding is not None and binding.provider_account_id != account.pk:
        raise ValidationError("The binding must belong to the provider account.")
    if job is not None and (binding is None or job.binding_id != binding.pk):
        raise ValidationError("The job must belong to the binding.")
    with transaction.atomic():
        account = ProviderAccount.objects.select_for_update().get(pk=account.pk)
        now += timedelta(seconds=time.monotonic() - started)
        if job is not None and job.retry_until is not None and now >= job.retry_until:
            raise HistoryWindowClosedError
        if account.authentication_failed_revision == account.credential_revision:
            raise RequestDeferredError(None)
        if account.request_blocked_until is not None and account.request_blocked_until > now:
            raise RequestDeferredError(account.request_blocked_until)
        utc_day = now.astimezone(UTC).date()
        used = RequestAttempt.objects.filter(provider_account=account, utc_day=utc_day).count()
        if not account.enabled or account.authorized_at is None or used >= account.daily_quota:
            raise QuotaExceededError
        if (
            job is not None
            and purpose == RequestPurpose.ESTIMATED_ACTUALS
            and used + 1 + remaining_forecast_requests(account, now) > account.daily_quota
        ):
            raise ForecastBudgetReservedError
        return RequestAttempt.objects.create(
            provider_account=account,
            binding=binding,
            job=job,
            utc_day=utc_day,
            created_at=now,
            purpose=purpose,
        )


def reserve_request(
    binding: SiteBinding, now: datetime, *, purpose: str, job: CollectionJob | None = None
) -> RequestAttempt:
    return reserve_account_request(
        binding.provider_account, now, purpose=purpose, binding=binding, job=job
    )


def record_provider_failure(account: ProviderAccount, error: ProviderError, now: datetime) -> None:
    if error.status_code not in {401, 429}:
        return
    with transaction.atomic():
        current = ProviderAccount.objects.select_for_update().get(pk=account.pk)
        if current.credential_revision != account.credential_revision:
            return
        if error.status_code == 401:
            current.authentication_failed_revision = account.credential_revision
        else:
            blocked_until = next_retry_at(now, 1, rate_limit_retry_after(error.retry_after_seconds))
            current.request_blocked_until = max(current.request_blocked_until or now, blocked_until)
        current.save(update_fields=("authentication_failed_revision", "request_blocked_until"))


def validate_binding(
    actor: User, binding_id: int, adapter: ProviderAdapter, now: datetime
) -> SiteBinding:
    started = time.monotonic()
    require_admin(actor)
    binding = SiteBinding.objects.select_related("provider_account").get(
        pk=binding_id, region__deleted_at__isnull=True
    )
    if binding.provider_account.provider == Provider.SOLCAST and (
        not binding.settings_verification_reference or binding.installation_date is None
    ):
        raise ValidationError("Confirmed site settings and installation date are required.")
    attempt = reserve_request(binding, now, purpose=RequestPurpose.VALIDATION)
    try:
        adapter.validate(binding)
    except ProviderError as exc:
        RequestAttempt.objects.filter(pk=attempt.pk).update(outcome=RequestOutcome.FAILED)
        SiteBinding.objects.filter(pk=binding.pk).update(health=BindingHealth.ERROR)
        record_provider_failure(
            binding.provider_account, exc, now + timedelta(seconds=time.monotonic() - started)
        )
        raise
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=binding.region_id)
        account = ProviderAccount.objects.select_for_update().get(pk=binding.provider_account_id)
        if (
            region.deleted_at is not None
            or region.revision != binding.region_revision
            or (account.credential_revision != binding.provider_account.credential_revision)
        ):
            raise ValidationError("Configuration changed during validation.")
        binding.validated_at = now
        binding.validated_credential_revision = account.credential_revision
        binding.health = BindingHealth.HEALTHY
        binding.save(update_fields=("validated_at", "validated_credential_revision", "health"))
        if (
            region.status == RegionStatus.APPROVED
            and region.approved_binding_id == binding.pk
            and account.enabled
            and account.authorized_at is not None
        ):
            recovered_at = now + timedelta(seconds=time.monotonic() - started)
            latest = (
                CollectionJob.objects.select_for_update()
                .filter(
                    binding=binding,
                    endpoint="forecasts",
                    reference_import__isnull=True,
                    scheduled_slot__lte=recovered_at,
                )
                .order_by("-scheduled_slot", "-pk")
                .first()
            )
            if (
                latest is not None
                and latest.status == "failed"
                and latest.last_error == "provider_authentication_failed"
            ):
                latest.status = "pending"
                latest.available_at = recovered_at
                latest.attempts = 0
                latest.last_error = ""
                latest.lease_token = None
                latest.lease_expires_at = None
                latest.save(
                    update_fields=(
                        "status",
                        "available_at",
                        "attempts",
                        "last_error",
                        "lease_token",
                        "lease_expires_at",
                    )
                )
        attempt.outcome = "succeeded"
        attempt.save(update_fields=("outcome",))
        AuditEvent.objects.create(
            actor=actor,
            region=region,
            action=AuditAction.CONNECTION_VALIDATED,
            revision=region.revision,
        )
    return binding
