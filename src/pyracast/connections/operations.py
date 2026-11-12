import time
from datetime import UTC, datetime, timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Value
from django.utils import timezone

from pyracast.accounts.operations import require_admin
from pyracast.connections.contracts import ProviderAdapter, ProviderFailure
from pyracast.connections.identity import canonical_account_identity
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.secrets import validate_secret_reference
from pyracast.forecasts.budget import remaining_forecast_requests
from pyracast.forecasts.models import CollectionJob, RequestAttempt
from pyracast.regions.models import AuditEvent, Region


class QuotaExhausted(Exception):
    pass


class ForecastBudgetReserved(QuotaExhausted):
    pass


class HistoryWindowClosed(QuotaExhausted):
    pass


def create_account(
    actor: User,
    *,
    provider: str,
    external_account_id: str,
    secret_reference: str,
    daily_quota: int = 10,
    authorization_reference: str = "",
) -> ProviderAccount:
    require_admin(actor)
    provider = provider.strip().casefold()
    identity = external_account_id.strip()
    if provider not in {"fixture", "solcast"} or not identity:
        raise ValidationError("Invalid provider account identity.")
    validate_secret_reference(secret_reference)
    if daily_quota < 1 or daily_quota > 10:
        raise ValidationError("Account quota must be between 1 and 10.")
    authorization_reference = authorization_reference.strip()
    if (provider == "solcast" and not authorization_reference) or (
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
                "secret_reference": secret_reference,
                "daily_quota": daily_quota,
                "authorization_reference": authorization_reference or "fixture-development-only",
                "authorized_by": actor,
                "authorized_at": timezone.now(),
            },
        )
        if created:
            account.refresh_from_db(fields=("provider", "external_account_id"))
            AuditEvent.objects.create(
                actor=actor,
                provider_account=account,
                action="provider_account_created",
                revision=account.credential_revision,
            )
    return account


def rotate_secret(actor: User, account_id: int, secret_reference: str) -> None:
    require_admin(actor)
    validate_secret_reference(secret_reference)
    with transaction.atomic():
        account = ProviderAccount.objects.select_for_update().get(pk=account_id)
        account.secret_reference = secret_reference
        account.credential_revision += 1
        account.save(update_fields=("secret_reference", "credential_revision"))
        AuditEvent.objects.create(
            actor=actor,
            provider_account=account,
            action="provider_secret_rotated",
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
            raise HistoryWindowClosed
        utc_day = now.astimezone(UTC).date()
        used = RequestAttempt.objects.filter(provider_account=account, utc_day=utc_day).count()
        if not account.enabled or account.authorized_at is None or used >= account.daily_quota:
            raise QuotaExhausted
        if (
            job is not None
            and job.retry_until is not None
            and purpose == "estimated_actuals"
            and used + 1 + remaining_forecast_requests(account, now) > account.daily_quota
        ):
            raise ForecastBudgetReserved
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


def validate_binding(
    actor: User, binding_id: int, adapter: ProviderAdapter, now: datetime
) -> SiteBinding:
    require_admin(actor)
    binding = SiteBinding.objects.select_related("provider_account").get(
        pk=binding_id, region__deleted_at__isnull=True
    )
    if binding.provider_account.provider == "solcast" and (
        not binding.settings_verification_reference or binding.installation_date is None
    ):
        raise ValidationError("Confirmed site settings and installation date are required.")
    attempt = reserve_request(binding, now, purpose="validation")
    try:
        adapter.validate(binding)
    except ProviderFailure:
        RequestAttempt.objects.filter(pk=attempt.pk).update(outcome="failed")
        SiteBinding.objects.filter(pk=binding.pk).update(health="error")
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
        binding.health = "healthy"
        binding.save(update_fields=("validated_at", "validated_credential_revision", "health"))
        attempt.outcome = "succeeded"
        attempt.save(update_fields=("outcome",))
        AuditEvent.objects.create(
            actor=actor, region=region, action="connection_validated", revision=region.revision
        )
    return binding
