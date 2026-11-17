import hashlib
import logging
import secrets
from datetime import timedelta

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from pyracast.accounts.language import user_language
from pyracast.accounts.models import (
    InvitationDeliveryStatus,
    Membership,
    Tenant,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.accounts.operations import require_workspace_admin

logger = logging.getLogger(__name__)


class InvitationRateLimitError(Exception):
    pass


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
        raise ValidationError(_("The invitation is invalid or expired."))
    return invitation


@transaction.atomic
def invite_workspace_member(actor: User, tenant_id: int, email: str) -> WorkspaceInvitation:
    require_workspace_admin(actor, tenant_id)
    email = email.strip().lower()
    validate_email(email)
    if Membership.objects.filter(tenant_id=tenant_id, user__email__iexact=email).exists():
        raise ValidationError(_("This account already belongs to the workspace."))
    now = timezone.now()
    recent = WorkspaceInvitation.objects.filter(tenant_id=tenant_id)
    if (
        recent.filter(created_at__gte=now - timedelta(hours=1)).count() >= 20
        or recent.filter(email=email, created_at__gte=now - timedelta(minutes=1)).exists()
    ):
        raise InvitationRateLimitError
    WorkspaceInvitation.objects.filter(
        tenant_id=tenant_id, email=email, accepted_at__isnull=True, expires_at__gt=now
    ).update(expires_at=now)
    token = secrets.token_urlsafe(32)
    invitation = WorkspaceInvitation.objects.create(
        tenant_id=tenant_id,
        email=email,
        token_hash=invitation_digest(token),
        created_by=actor,
        expires_at=now + timedelta(days=7),
    )
    tenant = Tenant.objects.get(pk=tenant_id)
    link = f"{settings.PYRACAST_PUBLIC_ORIGIN}/app/#token={token}"
    language = user_language(actor) or translation.get_language()

    def deliver() -> None:
        with translation.override(language):
            subject = _("Pyracast workspace invitation")
            context = {"workspace": tenant.name, "email": email, "action_url": link}
            body = render_to_string("mail/workspace_invitation.txt", context).strip()
            html = render_to_string("mail/workspace_invitation.html", context)
        try:
            sent = send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email], html_message=html)
        # A mail backend can accept the message before raising an arbitrary exception.
        except Exception:
            logger.warning(
                "Workspace invitation mail outcome is unknown; invitation_id=%s", invitation.pk
            )
            sent = 0
        invitation.delivery_status = (
            InvitationDeliveryStatus.SENT if sent == 1 else InvitationDeliveryStatus.UNCERTAIN
        )
        WorkspaceInvitation.objects.filter(pk=invitation.pk).update(
            delivery_status=invitation.delivery_status
        )

    transaction.on_commit(deliver)
    return invitation


@transaction.atomic
def accept_workspace_invitation(actor: User, token: str) -> Membership:
    invitation = WorkspaceInvitation.objects.filter(token_hash=invitation_digest(token)).first()
    if invitation is None:
        raise ValidationError(_("The invitation is invalid or expired."))
    Tenant.objects.select_for_update().get(pk=invitation.tenant_id)
    invitation = WorkspaceInvitation.objects.select_for_update().get(pk=invitation.pk)
    if invitation.accepted_at is not None or invitation.expires_at <= timezone.now():
        raise ValidationError(_("The invitation is invalid or expired."))
    if (
        not User.objects.filter(pk=actor.pk, is_active=True).exists()
        or not EmailAddress.objects.filter(
            user=actor, email__iexact=invitation.email, verified=True
        ).exists()
    ):
        raise PermissionDenied(_("Verify the invited email address before accepting."))
    membership, _created = Membership.objects.get_or_create(
        tenant_id=invitation.tenant_id, user=actor, defaults={"role": WorkspaceRole.VIEWER}
    )
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["accepted_at"])
    return membership


@transaction.atomic
def revoke_workspace_invitation(actor: User, tenant_id: int, invitation_id: int) -> None:
    require_workspace_admin(actor, tenant_id)
    invitation = (
        WorkspaceInvitation.objects.select_for_update()
        .filter(tenant_id=tenant_id, pk=invitation_id)
        .first()
    )
    if invitation is None:
        raise ValidationError(_("The invitation is invalid or expired."))
    invitation.expires_at = min(invitation.expires_at, timezone.now())
    invitation.save(update_fields=["expires_at"])
