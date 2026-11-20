import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy

from pyracast.accounts.authorization import authorize_workspace_admin, require_platform_admin
from pyracast.accounts.event_mail import enqueue_review_mail
from pyracast.connections.models import BindingHealth, Provider, ProviderAccount, SiteBinding
from pyracast.errors import ConflictError, DomainError, ErrorCode
from pyracast.forecasts.models import CollectionJob
from pyracast.plants.models import CONFIGURATION_PARAMETERS, Plant, PlantSubmission
from pyracast.plants.operations import activate_submitted_plants
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, Region, RegionStatus
from pyracast.regions.review import SETUP_CHANGED, require_seen_status, set_review_status

DECLARED_FIELDS = frozenset(CONFIGURATION_PARAMETERS)
UNDECLARED_FIELDS = frozenset({"id", "plant", "submitted_by", "created_at", "updated_at"})

DECLARATION_CHANGED = gettext_lazy(
    "The declared plant details changed after this page opened. Review them again before approving."
)
NOT_RESUBMITTABLE = gettext_lazy("Only a rejected plant can be resubmitted for review.")
REVIEW_REASON_MAX_LENGTH = 500


class ApprovalBlocker(StrEnum):
    NOT_VALIDATED = "not_validated"
    LAST_VALIDATION_FAILED = "last_validation_failed"
    KEY_REJECTED = "key_rejected"
    ACCOUNT_UNAVAILABLE = "account_unavailable"
    SETTINGS_UNCONFIRMED = "settings_unconfirmed"


BLOCKER_MESSAGES = {
    ApprovalBlocker.NOT_VALIDATED: gettext_lazy(
        "Validate the current setup and key revision before approval."
    ),
    ApprovalBlocker.LAST_VALIDATION_FAILED: gettext_lazy(
        "The last validation failed. Validate the connection again before approval."
    ),
    ApprovalBlocker.KEY_REJECTED: gettext_lazy(
        "The provider rejected this account's current key. Replace it, then validate again."
    ),
    ApprovalBlocker.ACCOUNT_UNAVAILABLE: gettext_lazy(
        "The provider account is disabled or not authorized for this service."
    ),
    ApprovalBlocker.SETTINGS_UNCONFIRMED: gettext_lazy(
        "Confirm the provider site settings and installation date before approval."
    ),
}


class ReviewDecision(models.TextChoices):
    REJECT = "reject"
    SUSPEND = "suspend"


DECISIONS = {
    ReviewDecision.REJECT: (RegionStatus.REJECTED, AuditAction.REGION_REJECTED),
    ReviewDecision.SUSPEND: (RegionStatus.SUSPENDED, AuditAction.REGION_SUSPENDED),
}


@dataclass(frozen=True)
class ReviewSnapshot:
    status: str
    revision: int
    declaration: str | None = None


@dataclass(frozen=True)
class Approval:
    job: CollectionJob
    changed: bool


def approval_blockers(binding: SiteBinding, account: ProviderAccount) -> list[ApprovalBlocker]:
    checks = {
        ApprovalBlocker.NOT_VALIDATED: binding.validated_at is None
        or binding.validated_credential_revision != account.credential_revision,
        ApprovalBlocker.LAST_VALIDATION_FAILED: binding.health == BindingHealth.ERROR,
        ApprovalBlocker.KEY_REJECTED: account.authentication_failed_revision
        == account.credential_revision,
        ApprovalBlocker.ACCOUNT_UNAVAILABLE: not account.enabled or account.authorized_at is None,
        ApprovalBlocker.SETTINGS_UNCONFIRMED: account.provider == Provider.SOLCAST
        and (not binding.settings_verification_reference or binding.installation_date is None),
    }
    return [blocker for blocker, blocked in checks.items() if blocked]


def declaration_fingerprint(region_id: int) -> str:
    rows = (
        PlantSubmission.objects.filter(plant__region_id=region_id)
        .order_by("plant_id")
        .values_list("plant_id", *sorted(DECLARED_FIELDS))
    )
    return hashlib.sha256(json.dumps([list(row) for row in rows]).encode()).hexdigest()


def review_snapshot(region_id: int) -> ReviewSnapshot:
    region = Region.objects.active().get(pk=region_id)
    return ReviewSnapshot(region.status, region.revision, declaration_fingerprint(region_id))


def _require_seen(region: Region, seen: ReviewSnapshot | None) -> None:
    if seen is None:
        return
    require_seen_status(region, seen.status)
    if region.revision != seen.revision:
        raise ConflictError(ErrorCode.CONFLICT, SETUP_CHANGED)


def approve_region(
    actor: User,
    region_id: int,
    binding_id: int,
    now: datetime,
    *,
    seen: ReviewSnapshot | None = None,
) -> Approval:
    """Approves the current binding. `seen` is what the operator's page showed; a console
    approval passes it so a later decision or declaration change refuses the approval."""
    require_platform_admin(actor)
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    with transaction.atomic():
        region = Region.objects.active().select_for_update().get(pk=region_id)
        binding = SiteBinding.objects.get(
            pk=binding_id, region=region, region_revision=region.revision
        )
        account = ProviderAccount.objects.select_for_update().get(pk=binding.provider_account_id)
        blockers = approval_blockers(binding, account)
        if blockers:
            raise ConflictError(ErrorCode.CONFLICT, BLOCKER_MESSAGES[blockers[0]])
        if region.status == RegionStatus.APPROVED and region.approved_binding_id == binding.pk:
            return Approval(
                CollectionJob.objects.filter(binding=binding).earliest("created_at"), False
            )
        _require_seen(region, seen)
        list(Plant.objects.select_for_update().filter(region=region))
        if seen is not None and seen.declaration != declaration_fingerprint(region.pk):
            raise ConflictError(ErrorCode.CONFLICT, DECLARATION_CHANGED)
        set_review_status(region, RegionStatus.APPROVED, binding=binding)
        activate_submitted_plants(actor, region, binding)
        event = record_audit(
            actor=actor,
            region=region,
            action=AuditAction.REGION_APPROVED,
            revision=region.revision,
            details={"binding_id": binding.pk},
        )
        enqueue_review_mail(region.tenant_id, event.pk, RegionStatus.APPROVED, now)
        job, _ = CollectionJob.objects.get_or_create(
            binding=binding,
            endpoint="forecasts",
            scheduled_for=now,
            defaults={
                "target_date": now.astimezone(ZoneInfo(region.timezone)).date() + timedelta(days=1),
                "available_at": now,
            },
        )
    return Approval(job, True)


def review_reason(reason: str) -> str:
    reason = reason.strip()
    if not reason:
        raise DomainError(ErrorCode.REVIEW_REASON_REQUIRED, field="reason")
    if len(reason) > REVIEW_REASON_MAX_LENGTH:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            gettext_lazy("Use at most 500 characters for the reason."),
            field="reason",
        )
    return reason


def decide_review(
    actor: User,
    region_id: int,
    decision: str,
    *,
    reason: str,
    seen: ReviewSnapshot | None = None,
) -> bool:
    require_platform_admin(actor)
    if decision not in ReviewDecision.values:
        raise ValidationError("Choose a review decision.")
    reason = review_reason(reason)
    target, action = DECISIONS[ReviewDecision(decision)]
    with transaction.atomic():
        region = Region.objects.active().select_for_update().get(pk=region_id)
        if region.status == target:
            return False
        _require_seen(region, seen)
        set_review_status(region, target)
        event = record_audit(
            actor=actor,
            region=region,
            action=action,
            revision=region.revision,
            details={"reason": reason},
        )
        enqueue_review_mail(region.tenant_id, event.pk, target, timezone.now())
    return True


def resubmit_region(actor: User, tenant_id: int, region_id: int, *, expected_revision: int) -> bool:
    """Returns a rejected plant to review; False when it is already in review. Only the
    operator's approval leaves review, so a resubmission never approves a plant."""
    with transaction.atomic():
        authorize_workspace_admin(actor, tenant_id)
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        if region.status == RegionStatus.PENDING:
            return False
        if region.status != RegionStatus.REJECTED:
            raise ConflictError(ErrorCode.CONFLICT, NOT_RESUBMITTABLE)
        if region.revision != expected_revision:
            raise ConflictError(ErrorCode.REGION_REVISION_CHANGED)
        set_review_status(region, RegionStatus.PENDING)
        record_audit(
            actor=actor,
            region=region,
            action=AuditAction.REGION_RESUBMITTED,
            revision=region.revision,
        )
    return True
