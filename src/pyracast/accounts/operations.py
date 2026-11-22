from datetime import datetime

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q, QuerySet
from django.db.models.functions import Lower
from django.utils import timezone

from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.authorization import authorize_workspace_admin, require_platform_admin
from pyracast.accounts.event_mail import enqueue_role_mail
from pyracast.accounts.models import (
    InvitationRevocation,
    Membership,
    Tenant,
    UserLanguagePreference,
    WorkspaceAdminChange,
    WorkspaceInvitation,
    WorkspaceInvitationQuerySet,
    WorkspaceMemberRemoval,
    WorkspaceRole,
)
from pyracast.accounts.processes import lock_account
from pyracast.errors import ConflictError, DomainError, ErrorCode


def register_account(user: User, *, language: str, workspace_name: str | None) -> None:
    if workspace_name is not None:
        tenant = Tenant.objects.create(name=workspace_name)
        Membership.objects.create(user=user, tenant=tenant, role=WorkspaceRole.ADMIN)
    UserLanguagePreference.objects.create(user=user, language=language)


def _membership(tenant_id: int, user_id: int) -> QuerySet[Membership]:
    return Membership.objects.filter(tenant_id=tenant_id, user_id=user_id)


def _refuse_administrator(actor: User, membership: Membership | None) -> None:
    if membership is not None and (
        membership.role == WorkspaceRole.ADMIN or membership.user_id == actor.pk
    ):
        raise DomainError(ErrorCode.WORKSPACE_ADMIN_NOT_REMOVABLE)


@transaction.atomic
def remove_workspace_member(
    actor: User, tenant_id: int, user_id: int, *, platform: bool = False
) -> bool:
    # A refused caller or target locks nothing. Then the member's account rows before the
    # workspace, the order of every transaction that locks a person and a workspace: an
    # acceptance or an address verified meanwhile is among what is read below, and the target is
    # checked again under those locks.
    authorize_workspace_admin(actor, tenant_id, platform=platform, lock=False)
    _refuse_administrator(actor, _membership(tenant_id, user_id).first())
    lock_account(user_id)
    authorize_workspace_admin(actor, tenant_id, platform=platform)
    membership = _membership(tenant_id, user_id).select_related("user").first()
    if membership is None:
        return False
    _refuse_administrator(actor, membership)
    addresses = list(EmailAddress.objects.filter(user_id=user_id).values_list("email", "verified"))
    primary = membership.user.email
    from pyracast.notifications.recipients import remove_member_recipients

    remove_member_recipients(
        tenant_id,
        membership.pk,
        {email for email, verified in addresses if verified} | {primary},
    )
    now = timezone.now()
    WorkspaceInvitation.objects.filter(tenant_id=tenant_id).pending(now).alias(
        normalized_email=Lower("email")
    ).filter(
        normalized_email__in=[email.lower() for email, _ in addresses] + [primary.lower()]
    ).revoke(now, InvitationRevocation.MEMBER_REMOVED)
    WorkspaceMemberRemoval.objects.create(tenant_id=tenant_id, actor=actor, user_id=user_id)
    membership.delete()
    record_security_event(
        SecurityEvent.WORKSPACE_MEMBER_REMOVED,
        user_id=actor.pk,
        tenant_id=tenant_id,
        target_id=user_id,
    )
    return True


def invitations_closed_by_reassignment(
    tenant_id: int, now: datetime
) -> WorkspaceInvitationQuerySet:
    # An expired administrator invitation closes too: no one may send it again.
    return WorkspaceInvitation.objects.filter(
        Q(expires_at__gt=now) | Q(role=WorkspaceRole.ADMIN), tenant_id=tenant_id
    ).open()


@transaction.atomic
def reassign_workspace_admin(
    actor: User, tenant_id: int, user_id: int, *, expected_admin_id: int | None
) -> bool:
    """False when the chosen member already administers the workspace; that is checked before
    the stale check, so a repeated submit is reported once and not refused."""
    require_platform_admin(actor)
    Tenant.objects.select_for_update().get(pk=tenant_id)
    membership = Membership.objects.filter(
        tenant_id=tenant_id, user_id=user_id, user__is_active=True
    ).first()
    if membership is None:
        raise DomainError(ErrorCode.MEMBER_UNAVAILABLE)
    if membership.role == WorkspaceRole.ADMIN:
        return False
    current_admin_id = (
        Membership.objects.filter(tenant_id=tenant_id, role=WorkspaceRole.ADMIN)
        .values_list("user_id", flat=True)
        .first()
    )
    if current_admin_id != expected_admin_id:
        raise ConflictError(ErrorCode.WORKSPACE_ADMIN_CHANGED)
    from pyracast.plants.automation import disable_workspace_automation

    disable_workspace_automation(actor, tenant_id)
    Membership.objects.filter(tenant_id=tenant_id, role=WorkspaceRole.ADMIN).update(
        role=WorkspaceRole.VIEWER
    )
    membership.role = WorkspaceRole.ADMIN
    membership.save(update_fields=["role"])
    now = timezone.now()
    invitations_closed_by_reassignment(tenant_id, now).revoke(
        now, InvitationRevocation.ADMINISTRATOR_CHANGED
    )
    change = WorkspaceAdminChange.objects.create(
        tenant_id=tenant_id,
        actor=actor,
        administrator_id=user_id,
        previous_administrator_id=current_admin_id,
    )
    enqueue_role_mail(change, now)
    record_security_event(
        SecurityEvent.WORKSPACE_ADMIN_REASSIGNED,
        user_id=actor.pk,
        tenant_id=tenant_id,
        target_id=user_id,
    )
    return True
