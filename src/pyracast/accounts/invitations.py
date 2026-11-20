import hashlib
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.db import connection, transaction
from django.db.models import BooleanField, Exists, OuterRef, Q, QuerySet, Value
from django.template.loader import render_to_string
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.audit import SecurityEvent, record_security_event, refusal_throttle
from pyracast.accounts.authorization import authorize_workspace_admin
from pyracast.accounts.language import user_language
from pyracast.accounts.mail_intents import enqueue_mail
from pyracast.accounts.models import (
    InvitationDeliveryStatus,
    InvitationRevocation,
    InvitationState,
    Membership,
    OutboundMailError,
    OutboundMailKind,
    Tenant,
    WorkspaceAdminChange,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.accounts.processes import lock_account
from pyracast.errors import ConflictError, DomainError, ErrorCode, ForbiddenError

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


@dataclass(frozen=True)
class InvitationPreview:
    workspace: str
    role: str
    inviter: str | None
    email: str
    expires_at: datetime


def invitation_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def pending_invitation(token: str, email: str) -> WorkspaceInvitation:
    invitation = (
        WorkspaceInvitation.objects.pending(timezone.now())
        .filter(token_hash=invitation_digest(token), email=email.strip().lower())
        .first()
    )
    if invitation is None:
        raise DomainError(ErrorCode.INVITATION_UNAVAILABLE)
    return invitation


def workspace_invitations(tenant_id: int) -> QuerySet[WorkspaceInvitation]:
    return (
        WorkspaceInvitation.objects.filter(tenant_id=tenant_id)
        .open()
        .order_by("-created_at", "-pk")
    )


def _usable_by(
    invitation: WorkspaceInvitation, viewer: User | AnonymousUser, *, viewer_is_member: bool
) -> bool:
    state = invitation.state_at(timezone.now())
    if state == InvitationState.PENDING:
        return True
    return (
        state == InvitationState.ACCEPTED
        and viewer.is_authenticated
        and invitation.accepted_by_id == viewer.pk
        and viewer_is_member
    )


def preview_workspace_invitation(token: str, viewer: User | AnonymousUser) -> InvitationPreview:
    # One lookup decides every refusal, so neither the answer nor the work done tells an
    # unknown token from a closed invitation, from one accepted by another account or from one
    # whose acceptor has left.
    viewer_is_member = (
        Exists(Membership.objects.filter(tenant_id=OuterRef("tenant_id"), user=viewer))
        if isinstance(viewer, User)
        else Value(False, output_field=BooleanField())
    )
    invitation = (
        WorkspaceInvitation.objects.select_related("tenant", "created_by")
        .annotate(viewer_is_member=viewer_is_member)
        .filter(token_hash=invitation_digest(token))
        .first()
    )
    if invitation is None or not _usable_by(
        invitation, viewer, viewer_is_member=invitation.viewer_is_member
    ):
        raise DomainError(ErrorCode.INVITATION_UNAVAILABLE)
    inviter = invitation.created_by
    named = Membership.objects.filter(
        tenant_id=invitation.tenant_id, user=inviter, user__is_active=True
    ).exists()
    return InvitationPreview(
        workspace=invitation.tenant.name,
        role=invitation.role,
        inviter=(inviter.email or None) if named else None,
        email=invitation.email,
        expires_at=invitation.expires_at,
    )


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
    # An invitation whose mail failed or may not have arrived can be reissued at once; the longer
    # budgets still count it.
    budgets = (
        (WORKSPACE_HOURLY, invitations.filter(tenant_id=tenant_id)),
        (
            RESEND,
            to_address.filter(tenant_id=tenant_id).exclude(
                delivery_status__in=(
                    InvitationDeliveryStatus.UNCERTAIN,
                    InvitationDeliveryStatus.FAILED,
                )
            ),
        ),
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
    # Expired invitations are superseded too: one invitation per address stays open.
    WorkspaceInvitation.objects.filter(replaced, tenant_id=tenant_id).revoke(
        now, InvitationRevocation.SUPERSEDED
    )
    invitation = WorkspaceInvitation.objects.create(
        tenant_id=tenant_id,
        email=email,
        created_by=actor,
        expires_at=now + INVITATION_LIFETIME,
        role=role,
    )
    record_security_event(
        SecurityEvent.INVITATION_SENT,
        user_id=actor.pk,
        tenant_id=tenant_id,
        target_id=invitation.pk,
        address=email,
    )
    enqueue_mail(
        OutboundMailKind.WORKSPACE_INVITATION,
        email,
        invitation=invitation,
        language=language or user_language(actor) or translation.get_language(),
        expires_at=invitation.expires_at,
        now=now,
    )
    return invitation


@sensitive_variables("token", "context")
def compose_invitation(
    invitation_id: int, language: str, now: datetime
) -> EmailMultiAlternatives | OutboundMailError:
    """Mints the token while the mailer holds the intent: only its digest is stored, and the
    link exists only in the message. A send that fails before transmission mints a new one."""
    invitation = (
        WorkspaceInvitation.objects.select_for_update(of=("self",))
        .select_related("tenant")
        .filter(pk=invitation_id)
        .pending(now)
        .first()
    )
    if invitation is None:
        return OutboundMailError.INVITATION_UNAVAILABLE
    token = secrets.token_urlsafe(32)
    invitation.token_hash = invitation_digest(token)
    invitation.save(update_fields=["token_hash"])
    with translation.override(language):
        context = {
            "workspace": invitation.tenant.name,
            "email": invitation.email,
            "action_url": f"{settings.PYRACAST_PUBLIC_ORIGIN}/app/invitation/#token={token}",
            "administrator": invitation.role == WorkspaceRole.ADMIN,
        }
        message = EmailMultiAlternatives(
            _("Pyracast workspace invitation"),
            render_to_string("mail/workspace_invitation.txt", context).strip(),
            settings.DEFAULT_FROM_EMAIL,
            [invitation.email],
        )
        message.attach_alternative(
            render_to_string("mail/workspace_invitation.html", context), "text/html"
        )
    return message


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


def _refuse_acceptance(actor: User, invitation: WorkspaceInvitation, reason: str) -> NoReturn:
    _record_refused_acceptance(actor, invitation, reason)
    raise DomainError(ErrorCode.INVITATION_UNAVAILABLE)


def _accepted_before(actor: User, invitation: WorkspaceInvitation) -> Membership:
    # A retry after a lost response gets the first answer: only the accepting account, and
    # only while it is still a member.
    membership = (
        Membership.objects.filter(tenant_id=invitation.tenant_id, user=actor).first()
        if invitation.accepted_by_id == actor.pk
        else None
    )
    if membership is None:
        _refuse_acceptance(actor, invitation, InvitationState.ACCEPTED)
    return membership


def _holds_invited_address(actor: User, email: str) -> bool:
    return (
        User.objects.filter(pk=actor.pk, is_active=True).exists()
        and EmailAddress.objects.filter(user=actor, email__iexact=email, verified=True).exists()
    )


@transaction.atomic
def accept_workspace_invitation(actor: User, token: str) -> Membership:
    # The account's rows before the workspace, the order of the account processes: the invited
    # address is checked against the addresses this acceptance commits with.
    lock_account(actor.pk)
    found = (
        WorkspaceInvitation.objects.filter(token_hash=invitation_digest(token))
        .values_list("pk", "tenant_id")
        .first()
    )
    if found is None:
        raise DomainError(ErrorCode.INVITATION_UNAVAILABLE)
    invitation_id, tenant_id = found
    Tenant.objects.select_for_update().get(pk=tenant_id)
    invitation = WorkspaceInvitation.objects.select_for_update().get(pk=invitation_id)
    if invitation.accepted_at is not None:
        return _accepted_before(actor, invitation)
    if (state := invitation.state_at(timezone.now())) != InvitationState.PENDING:
        _refuse_acceptance(actor, invitation, state)
    admin_role = invitation.role == WorkspaceRole.ADMIN
    if admin_role and has_workspace_admin(tenant_id):
        _refuse_acceptance(actor, invitation, "administrator_exists")
    if not _holds_invited_address(actor, invitation.email):
        _record_refused_acceptance(actor, invitation, "email_unverified")
        raise ForbiddenError(ErrorCode.INVITATION_EMAIL_UNVERIFIED)
    membership, _created = Membership.objects.get_or_create(
        tenant_id=tenant_id, user=actor, defaults={"role": invitation.role}
    )
    if admin_role:
        membership.role = WorkspaceRole.ADMIN
        membership.save(update_fields=["role"])
        WorkspaceAdminChange.objects.create(
            tenant_id=tenant_id,
            actor_id=invitation.created_by_id,
            administrator=actor,
        )
        record_security_event(
            SecurityEvent.WORKSPACE_ADMIN_REASSIGNED,
            user_id=invitation.created_by_id,
            tenant_id=tenant_id,
            target_id=actor.pk,
        )
    invitation.accepted_at = timezone.now()
    invitation.accepted_by = actor
    invitation.save(update_fields=["accepted_at", "accepted_by"])
    record_security_event(
        SecurityEvent.INVITATION_ACCEPTED,
        user_id=actor.pk,
        tenant_id=tenant_id,
        target_id=invitation.pk,
    )
    return membership


@transaction.atomic
def revoke_workspace_invitation(
    actor: User, tenant_id: int, invitation_id: int, *, platform: bool = False
) -> bool:
    """False when the invitation was already closed."""
    authorize_workspace_admin(actor, tenant_id, platform=platform)
    invitation = (
        WorkspaceInvitation.objects.select_for_update()
        .filter(tenant_id=tenant_id, pk=invitation_id)
        .first()
    )
    if invitation is None or invitation.accepted_at is not None:
        raise DomainError(ErrorCode.INVITATION_UNAVAILABLE)
    if invitation.revoked_at is not None:
        return False
    invitation.revoked_at = timezone.now()
    invitation.revoked_reason = InvitationRevocation.CANCELLED
    invitation.save(update_fields=["revoked_at", "revoked_reason"])
    record_security_event(
        SecurityEvent.INVITATION_REVOKED,
        user_id=actor.pk,
        tenant_id=tenant_id,
        target_id=invitation.pk,
    )
    return True


@transaction.atomic
def reissue_workspace_invitation(
    actor: User,
    tenant_id: int,
    invitation_id: int,
    *,
    platform: bool = False,
    language: str | None = None,
) -> WorkspaceInvitation:
    authorize_workspace_admin(actor, tenant_id, platform=platform)
    invitation = (
        WorkspaceInvitation.objects.filter(tenant_id=tenant_id, pk=invitation_id).open().first()
    )
    if invitation is None:
        raise DomainError(ErrorCode.INVITATION_UNAVAILABLE)
    return invite_workspace_member(
        actor,
        tenant_id,
        invitation.email,
        role=invitation.role,
        language=language,
        platform=platform,
    )
