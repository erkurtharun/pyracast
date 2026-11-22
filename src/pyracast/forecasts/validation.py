import logging
import time
from datetime import datetime, timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext

from pyracast.accounts.authorization import require_platform_admin
from pyracast.connections.contracts import ProviderAdapter, ProviderError
from pyracast.connections.models import BindingHealth, Provider, ProviderAccount, SiteBinding
from pyracast.errors import ConflictError, ErrorCode
from pyracast.forecasts.budget import validations_in_flight
from pyracast.forecasts.models import (
    CollectionJobStatus,
    CollectionSchedule,
    RequestAttempt,
    RequestOutcome,
    RequestPurpose,
)
from pyracast.forecasts.quota import record_provider_failure, reserve_request
from pyracast.forecasts.timing import (
    armed_again_by_validation,
    forecast_timing,
    latest_forecast_job,
    latest_forecast_slot,
)
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, Region, RegionStatus, active_region

logger = logging.getLogger(__name__)


def refuses_binding(error: ProviderError) -> bool:
    """Only an unreachable, throttled or failing provider leaves the stored health as it was."""
    transient = error.code == "provider_request_failed" and error.retryable
    return not transient or error.status_code in {401, 403, 404}


def validate_binding(
    actor: User,
    binding_id: int,
    adapter: ProviderAdapter,
    now: datetime,
    *,
    credential_revision: int | None = None,
) -> SiteBinding:
    """`credential_revision` names the key the caller validates for; another one refuses."""
    started = time.monotonic()
    require_platform_admin(actor)
    binding = SiteBinding.objects.select_related("provider_account").get(
        active_region("region__"), pk=binding_id
    )
    if credential_revision not in {None, binding.provider_account.credential_revision}:
        raise ValidationError("Configuration changed during validation.")
    if binding.provider_account.provider == Provider.SOLCAST and (
        not binding.settings_verification_reference or binding.installation_date is None
    ):
        raise ValidationError(
            gettext("Confirmed site settings and installation date are required.")
        )
    adapter.prepare(binding)
    with transaction.atomic():
        current = Region.objects.active().select_for_update().filter(pk=binding.region_id).first()
        if current is None or current.revision != binding.region_revision:
            raise ConflictError(
                ErrorCode.CONFLICT, gettext("This provider binding is no longer current.")
            )
        if validations_in_flight(now).filter(binding=binding).exists():
            raise ConflictError(
                ErrorCode.CONFLICT,
                gettext(
                    "This connection is being validated now. "
                    "Reload the page in a minute to see the result."
                ),
            )
        # Validating an approved plant never takes a request a current plant's forecast needs.
        attempt = reserve_request(
            binding,
            now,
            purpose=RequestPurpose.VALIDATION,
            keep_forecast_reserve=current.status == RegionStatus.APPROVED,
            credential_revision=binding.provider_account.credential_revision,
        )
    try:
        adapter.validate(binding)
    except ProviderError as exc:
        logger.warning(
            "provider_validation_failed provider=%s binding_id=%s error_code=%s status_code=%s",
            binding.provider_account.provider,
            binding.pk,
            exc.code,
            exc.status_code,
        )
        RequestAttempt.objects.filter(pk=attempt.pk).update(
            outcome=RequestOutcome.FAILED, http_status=exc.status_code
        )
        if refuses_binding(exc):
            _record_refusal(binding)
        record_audit(
            actor=actor,
            region=binding.region,
            action=AuditAction.CONNECTION_VALIDATION_FAILED,
            revision=binding.region_revision,
            details={
                "binding_id": binding.pk,
                "error_code": exc.code,
                "status_code": exc.status_code,
            },
        )
        record_provider_failure(
            binding.provider_account, exc, now + timedelta(seconds=time.monotonic() - started)
        )
        raise
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=binding.region_id)
        account = ProviderAccount.objects.select_for_update().get(pk=binding.provider_account_id)
        # Under the account lock, so a reservation sees this validation in flight or its plant
        # validated (budget.remaining_forecast_requests).
        RequestAttempt.objects.filter(pk=attempt.pk).update(outcome=RequestOutcome.SUCCEEDED)
        if (
            region.deleted_at is None
            and region.revision == binding.region_revision
            and account.credential_revision == binding.provider_account.credential_revision
        ):
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
                _rearm_after_validation(
                    binding, region, now + timedelta(seconds=time.monotonic() - started)
                )
            record_audit(
                actor=actor,
                region=region,
                action=AuditAction.CONNECTION_VALIDATED,
                revision=region.revision,
                details={"binding_id": binding.pk},
            )
            return binding
    raise ValidationError("Configuration changed during validation.")


def _record_refusal(binding: SiteBinding) -> None:
    # A refusal of a superseded key never overwrites the health a newer key established.
    with transaction.atomic():
        account = ProviderAccount.objects.select_for_update().get(pk=binding.provider_account_id)
        if account.credential_revision == binding.provider_account.credential_revision:
            SiteBinding.objects.filter(pk=binding.pk).update(health=BindingHealth.ERROR)


def _rearm_after_validation(binding: SiteBinding, region: Region, recovered_at: datetime) -> None:
    timing = forecast_timing(binding, CollectionSchedule.objects.filter(region=region).first())
    due = (
        None
        if timing is None
        else latest_forecast_slot(
            recovered_at, region.timezone, timing.times, timing.effective_from
        )
    )
    latest = latest_forecast_job(binding.pk, recovered_at).select_for_update().first()
    if latest is not None and armed_again_by_validation(latest, due):
        latest.status = CollectionJobStatus.PENDING
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
