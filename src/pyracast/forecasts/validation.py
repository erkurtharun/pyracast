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
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobError,
    CollectionJobStatus,
    RequestAttempt,
    RequestOutcome,
    RequestPurpose,
)
from pyracast.forecasts.quota import record_provider_failure, reserve_request
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, Region, RegionStatus, active_region

logger = logging.getLogger(__name__)

# An unanswered validation attempt younger than this is still in flight: it outlasts the
# whole-request read deadline of a provider answer (outbound.READ_DEADLINE_SECONDS).
VALIDATION_IN_FLIGHT = timedelta(minutes=1)


def refuses_binding(error: ProviderError) -> bool:
    """Only an unreachable, throttled or failing provider leaves the stored health as it was."""
    transient = error.code == "provider_request_failed" and error.retryable
    return not transient or error.status_code in {401, 403, 404}


def validate_binding(
    actor: User, binding_id: int, adapter: ProviderAdapter, now: datetime
) -> SiteBinding:
    started = time.monotonic()
    require_platform_admin(actor)
    binding = SiteBinding.objects.select_related("provider_account").get(
        active_region("region__"), pk=binding_id
    )
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
        if RequestAttempt.objects.filter(
            binding=binding,
            purpose=RequestPurpose.VALIDATION,
            outcome=RequestOutcome.UNCERTAIN,
            created_at__gt=now - VALIDATION_IN_FLIGHT,
        ).exists():
            raise ConflictError(
                ErrorCode.CONFLICT,
                gettext(
                    "This connection is being validated now. "
                    "Reload the page in a minute to see the result."
                ),
            )
        attempt = reserve_request(binding, now, purpose=RequestPurpose.VALIDATION)
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
            SiteBinding.objects.filter(pk=binding.pk).update(health=BindingHealth.ERROR)
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
    RequestAttempt.objects.filter(pk=attempt.pk).update(outcome=RequestOutcome.SUCCEEDED)
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
            _rearm_after_validation(binding, now + timedelta(seconds=time.monotonic() - started))
        record_audit(
            actor=actor,
            region=region,
            action=AuditAction.CONNECTION_VALIDATED,
            revision=region.revision,
            details={"binding_id": binding.pk},
        )
    return binding


def _rearm_after_validation(binding: SiteBinding, recovered_at: datetime) -> None:
    latest = (
        CollectionJob.objects.select_for_update()
        .filter(
            binding=binding,
            endpoint=CollectionEndpoint.FORECASTS,
            reference_import__isnull=True,
            scheduled_for__lte=recovered_at,
        )
        .order_by("-scheduled_for", "-pk")
        .first()
    )
    if (
        latest is not None
        and latest.status == CollectionJobStatus.FAILED
        and latest.last_error == CollectionJobError.PROVIDER_AUTHENTICATION_FAILED
    ):
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
