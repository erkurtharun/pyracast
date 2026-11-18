from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models.functions import Lower
from django.utils import timezone

from pyracast.accounts.audit import SecurityEvent, record_on_commit
from pyracast.accounts.authorization import authorize_workspace_admin, require_platform_admin
from pyracast.accounts.models import (
    Membership,
    Tenant,
    UserLanguagePreference,
    WorkspaceAdminChange,
    WorkspaceInvitation,
    WorkspaceMemberRemoval,
    WorkspaceRole,
)
from pyracast.errors import ConflictError, DomainError, ErrorCode


def register_account(user: User, *, language: str, workspace_name: str | None) -> None:
    if workspace_name is not None:
        tenant = Tenant.objects.create(name=workspace_name)
        Membership.objects.create(user=user, tenant=tenant, role=WorkspaceRole.ADMIN)
    UserLanguagePreference.objects.create(user=user, language=language)


@transaction.atomic
def remove_workspace_member(
    actor: User, tenant_id: int, user_id: int, *, platform: bool = False
) -> None:
    authorize_workspace_admin(actor, tenant_id, platform=platform)
    membership = Membership.objects.filter(tenant_id=tenant_id, user_id=user_id).first()
    if membership is None:
        return
    if membership.role == WorkspaceRole.ADMIN or user_id == actor.pk:
        raise DomainError(ErrorCode.WORKSPACE_ADMIN_NOT_REMOVABLE)
    emails = set(EmailAddress.objects.filter(user_id=user_id).values_list("email", flat=True))
    emails.add(membership.user.email)
    from pyracast.notifications.recipients import remove_member_recipients

    verified_emails = set(
        EmailAddress.objects.filter(user_id=user_id, verified=True).values_list("email", flat=True)
    )
    verified_emails.add(membership.user.email)
    remove_member_recipients(tenant_id, membership.pk, verified_emails)
    now = timezone.now()
    WorkspaceInvitation.objects.filter(
        tenant_id=tenant_id, accepted_at__isnull=True, expires_at__gt=now
    ).alias(normalized_email=Lower("email")).filter(
        normalized_email__in=[email.lower() for email in emails]
    ).update(expires_at=now, revoked_at=now)
    WorkspaceMemberRemoval.objects.create(tenant_id=tenant_id, actor=actor, user_id=user_id)
    membership.delete()
    record_on_commit(
        SecurityEvent.WORKSPACE_MEMBER_REMOVED,
        user_id=actor.pk,
        tenant_id=tenant_id,
        target_id=user_id,
    )


@transaction.atomic
def reassign_workspace_admin(
    actor: User, tenant_id: int, user_id: int, *, expected_admin_id: int | None
) -> Membership:
    require_platform_admin(actor)
    Tenant.objects.select_for_update().get(pk=tenant_id)
    current_admin_id = (
        Membership.objects.filter(tenant_id=tenant_id, role=WorkspaceRole.ADMIN)
        .values_list("user_id", flat=True)
        .first()
    )
    if current_admin_id != expected_admin_id:
        raise ConflictError(ErrorCode.WORKSPACE_ADMIN_CHANGED)
    membership = Membership.objects.filter(
        tenant_id=tenant_id, user_id=user_id, user__is_active=True
    ).first()
    if membership is None:
        raise DomainError(ErrorCode.MEMBER_UNAVAILABLE)
    if membership.role != WorkspaceRole.ADMIN:
        from pyracast.plants.automation import disable_workspace_automation

        disable_workspace_automation(actor, tenant_id)
        Membership.objects.filter(tenant_id=tenant_id, role=WorkspaceRole.ADMIN).update(
            role=WorkspaceRole.VIEWER
        )
        membership.role = WorkspaceRole.ADMIN
        membership.save(update_fields=["role"])
        now = timezone.now()
        WorkspaceInvitation.objects.filter(
            tenant_id=tenant_id, accepted_at__isnull=True, expires_at__gt=now
        ).update(expires_at=now, revoked_at=now)
        WorkspaceAdminChange.objects.create(
            tenant_id=tenant_id,
            actor=actor,
            administrator_id=user_id,
            previous_administrator_id=current_admin_id,
        )
        record_on_commit(
            SecurityEvent.WORKSPACE_ADMIN_REASSIGNED,
            user_id=actor.pk,
            tenant_id=tenant_id,
            target_id=user_id,
        )
    return membership
