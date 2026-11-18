import hashlib
import logging
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.db import connection, transaction
from django.db.models import Q, QuerySet
from django.template.loader import render_to_string
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from pyracast.accounts.audit import (
    SecurityEvent,
    record_on_commit,
    record_security_event,
    refusal_throttle,
)
from pyracast.accounts.authorization import authorize_workspace_admin
from pyracast.accounts.language import user_language
from pyracast.accounts.models import (
    InvitationDeliveryStatus,
    Membership,
    Tenant,
    WorkspaceAdminChange,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.errors import ConflictError, DomainError, ErrorCode, ForbiddenError
from pyracast.mail.errors import NotSentError
from pyracast.mail.send import send

logger = logging.getLogger(__name__)

INVITATION_LIFETIME = timedelta(days=7)
# Two-key advisory lock space ("PYIV") that serializes budget checks per sender and per address.
INVITATION_LOCK_SPACE = 0x50594956


@dataclass(frozen=True)
class InvitationBudget:
    reason: str
    window: timedelta
    limit: int


WORKSPACE_HOURLY = InvitationBudget("workspace", timedelta(hours=1), 20)
RESEND = InvitationBudget("resend", timedelta(minutes=1), 1)
SENDER_DAILY = InvitationBudget("sender", timedelta(days=1), 50)
RECIPIENT_DAILY = InvitationBudget("recipient", timedelta(days=1), 3)
RECIPIENT_WORKSPACES = InvitationBudget("recipient_workspace", timedelta(days=1), 1)


def invitation_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def pending_invitation(token: str, email: str) -> WorkspaceInvitation:
    invitation = WorkspaceInvitation.objects.filter(
        token_hash=invitation_digest(token),
        email=email.strip().lower(),
        accepted_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()
    if invitation is None:
        raise DomainError(ErrorCode.INVITATION_UNAVAILABLE)
    return invitation


def _seconds_until_free(
    budget: InvitationBudget, invitations: QuerySet[WorkspaceInvitation], now: datetime
) -> int | None:
    # Revoked and accepted rows count too: the budget bounds mail sent, not invitations open.
    blocking = (
        invitations.filter(created_at__gt=now - budget.window)
        .order_by("-created_at")
        .values_list("created_at", flat=True)[budget.limit - 1 : budget.limit]
    )
    created_at = next(iter(blocking), None)
    if created_at is None:
        return None
    return max(1, math.ceil((created_at + budget.window - now).total_seconds()))


def _check_budgets(actor: User, tenant_id: int, email: str, now: datetime) -> None:
    with connection.cursor() as cursor:
        for key in (f"sender:{actor.pk}", f"recipient:{email}"):
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", [INVITATION_LOCK_SPACE, key]
            )
    invitations = WorkspaceInvitation.objects.all()
    to_address = invitations.filter(email=email)
    budgets = (
        (WORKSPACE_HOURLY, invitations.filter(tenant_id=tenant_id)),
        (RESEND, to_address.filter(tenant_id=tenant_id)),
        (SENDER_DAILY, invitations.filter(created_by=actor)),
        (RECIPIENT_DAILY, to_address),
        (RECIPIENT_WORKSPACES, to_address.exclude(tenant_id=tenant_id)),
    )
    waits = {
        budget.reason: wait
        for budget, rows in budgets
        if (wait := _seconds_until_free(budget, rows, now)) is not None
    }
    if not waits:
        return
    reason = max(waits, key=waits.__getitem__)
    record_security_event(
        SecurityEvent.INVITATION_LIMITED,
        user_id=actor.pk,
        tenant_id=tenant_id,
        address=email,
        reason=reason,
    )
    raise DomainError(ErrorCode.INVITATION_RATE_LIMITED, status=429, retry_after=waits[reason])


def has_workspace_admin(tenant_id: int) -> bool:
    return Membership.objects.filter(tenant_id=tenant_id, role=WorkspaceRole.ADMIN).exists()


@transaction.atomic
def invite_workspace_member(
    actor: User,
    tenant_id: int,
    email: str,
    *,
    role: str = WorkspaceRole.VIEWER,
    language: str | None = None,
    platform: bool = False,
) -> WorkspaceInvitation:
    admin_role = role == WorkspaceRole.ADMIN
    if admin_role and not platform:
        raise ForbiddenError(ErrorCode.PLATFORM_ADMIN_REQUIRED)
    authorize_workspace_admin(actor, tenant_id, platform=platform)
    if admin_role and has_workspace_admin(tenant_id):
        raise ConflictError(ErrorCode.WORKSPACE_ADMIN_CHANGED)
    email = email.strip().lower()
    validate_email(email)
    if Membership.objects.filter(tenant_id=tenant_id, user__email__iexact=email).exists():
        raise DomainError(ErrorCode.INVITATION_MEMBER_EXISTS)
    now = timezone.now()
    _check_budgets(actor, tenant_id, email, now)
    replaced = Q(email=email) | Q(role=WorkspaceRole.ADMIN) if admin_role else Q(email=email)
    WorkspaceInvitation.objects.filter(
        replaced, tenant_id=tenant_id, accepted_at__isnull=True, expires_at__gt=now
    ).update(expires_at=now)
    token = secrets.token_urlsafe(32)
    invitation = WorkspaceInvitation.objects.create(
        tenant_id=tenant_id,
        email=email,
        token_hash=invitation_digest(token),
        created_by=actor,
        expires_at=now + INVITATION_LIFETIME,
        role=role,
    )
    record_on_commit(
        SecurityEvent.INVITATION_SENT,
        user_id=actor.pk,
        tenant_id=tenant_id,
        target_id=invitation.pk,
        address=email,
    )
    tenant = Tenant.objects.get(pk=tenant_id)
    link = f"{settings.PYRACAST_PUBLIC_ORIGIN}/app/#token={token}"
    with translation.override(language or user_language(actor) or translation.get_language()):
        context = {
            "workspace": tenant.name,
            "email": email,
            "action_url": link,
            "administrator": admin_role,
        }
        message = EmailMultiAlternatives(
            _("Pyracast workspace invitation"),
            render_to_string("mail/workspace_invitation.txt", context).strip(),
            settings.DEFAULT_FROM_EMAIL,
            [email],
        )
        message.attach_alternative(
            render_to_string("mail/workspace_invitation.html", context), "text/html"
        )

    def deliver() -> None:
        try:
            send(message)
        except NotSentError as error:
            status = InvitationDeliveryStatus.FAILED
            logger.warning(
                "invitation_mail_not_sent invitation_id=%s error_code=%s", invitation.pk, error.code
            )
        except Exception as error:
            status = InvitationDeliveryStatus.UNCERTAIN
            logger.warning(
                "invitation_mail_uncertain invitation_id=%s error_type=%s",
                invitation.pk,
                type(error).__name__,
            )
        else:
            status = InvitationDeliveryStatus.SENT
        invitation.delivery_status = status
        WorkspaceInvitation.objects.filter(pk=invitation.pk).update(delivery_status=status)

    transaction.on_commit(deliver)
    return invitation


def _record_refused_acceptance(actor: User, invitation: WorkspaceInvitation, reason: str) -> None:
    # Like other signed-in refusals: one line a minute per actor, invitation and reason.
    if refusal_throttle.admit((actor.pk, f"invitation:{invitation.pk}", reason)):
        record_security_event(
            SecurityEvent.INVITATION_DENIED,
            user_id=actor.pk,
            tenant_id=invitation.tenant_id,
            target_id=invitation.pk,
            reason=reason,
        )


@transaction.atomic
def accept_workspace_invitation(actor: User, token: str) -> Membership:
    invitation = WorkspaceInvitation.objects.filter(token_hash=invitation_digest(token)).first()
    if invitation is None:
        raise DomainError(ErrorCode.INVITATION_UNAVAILABLE)
    Tenant.objects.select_for_update().get(pk=invitation.tenant_id)
    invitation = WorkspaceInvitation.objects.select_for_update().get(pk=invitation.pk)
    admin_role = invitation.role == WorkspaceRole.ADMIN
    if invitation.accepted_at is not None or invitation.expires_at <= timezone.now():
        reason = "accepted" if invitation.accepted_at is not None else "expired"
        _record_refused_acceptance(actor, invitation, reason)
        raise DomainError(ErrorCode.INVITATION_UNAVAILABLE)
    if admin_role and has_workspace_admin(invitation.tenant_id):
        _record_refused_acceptance(actor, invitation, "administrator_exists")
        raise DomainError(ErrorCode.INVITATION_UNAVAILABLE)
    if (
        not User.objects.filter(pk=actor.pk, is_active=True).exists()
        or not EmailAddress.objects.filter(
            user=actor, email__iexact=invitation.email, verified=True
        ).exists()
    ):
        _record_refused_acceptance(actor, invitation, "email_unverified")
        raise ForbiddenError(ErrorCode.INVITATION_EMAIL_UNVERIFIED)
    membership, _created = Membership.objects.get_or_create(
        tenant_id=invitation.tenant_id, user=actor, defaults={"role": invitation.role}
    )
    if admin_role:
        membership.role = WorkspaceRole.ADMIN
        membership.save(update_fields=["role"])
        WorkspaceAdminChange.objects.create(
            tenant_id=invitation.tenant_id,
            actor_id=invitation.created_by_id,
            administrator=actor,
        )
        record_on_commit(
            SecurityEvent.WORKSPACE_ADMIN_REASSIGNED,
            user_id=invitation.created_by_id,
            tenant_id=invitation.tenant_id,
            target_id=actor.pk,
        )
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["accepted_at"])
    record_on_commit(
        SecurityEvent.INVITATION_ACCEPTED,
        user_id=actor.pk,
        tenant_id=invitation.tenant_id,
        target_id=invitation.pk,
    )
    return membership


@transaction.atomic
def revoke_workspace_invitation(
    actor: User, tenant_id: int, invitation_id: int, *, platform: bool = False
) -> None:
    authorize_workspace_admin(actor, tenant_id, platform=platform)
    invitation = (
        WorkspaceInvitation.objects.select_for_update()
        .filter(tenant_id=tenant_id, pk=invitation_id)
        .first()
    )
    if invitation is None:
        raise DomainError(ErrorCode.INVITATION_UNAVAILABLE)
    invitation.expires_at = min(invitation.expires_at, timezone.now())
    invitation.save(update_fields=["expires_at"])
    record_on_commit(
        SecurityEvent.INVITATION_REVOKED,
        user_id=actor.pk,
        tenant_id=tenant_id,
        target_id=invitation.pk,
    )


@transaction.atomic
def reissue_workspace_invitation(
    actor: User, tenant_id: int, invitation_id: int, *, language: str | None = None
) -> WorkspaceInvitation:
    authorize_workspace_admin(actor, tenant_id, platform=True)
    invitation = WorkspaceInvitation.objects.filter(
        tenant_id=tenant_id,
        pk=invitation_id,
        accepted_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()
    if invitation is None:
        raise DomainError(ErrorCode.INVITATION_UNAVAILABLE)
    return invite_workspace_member(
        actor, tenant_id, invitation.email, role=invitation.role, language=language, platform=True
    )
