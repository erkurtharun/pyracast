from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext as _

from pyracast.accounts.authorization import require_platform_admin, tenant_membership
from pyracast.accounts.models import (
    Membership,
    Tenant,
    WorkspaceAdminChange,
    WorkspaceInvitation,
    WorkspaceMemberRemoval,
)


@transaction.atomic
def remove_workspace_member(actor: User, tenant_id: int, user_id: int) -> None:
    tenant_membership(actor, tenant_id, role=Membership.Role.ADMIN, lock=True)
    membership = Membership.objects.filter(tenant_id=tenant_id, user_id=user_id).first()
    if membership is None:
        return
    if membership.role == Membership.Role.ADMIN or user_id == actor.pk:
        raise ValidationError(_("The workspace administrator cannot be removed."))
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
    ).update(expires_at=now)
    WorkspaceMemberRemoval.objects.create(tenant_id=tenant_id, actor=actor, user_id=user_id)
    membership.delete()


@transaction.atomic
def reassign_workspace_admin(
    actor: User, tenant_id: int, user_id: int, *, expected_admin_id: int | None
) -> Membership:
    require_platform_admin(actor)
    Tenant.objects.select_for_update().get(pk=tenant_id)
    current_admin_id = (
        Membership.objects.filter(tenant_id=tenant_id, role=Membership.Role.ADMIN)
        .values_list("user_id", flat=True)
        .first()
    )
    if current_admin_id != expected_admin_id:
        raise ValidationError(_("The workspace administrator changed. Reload before saving."))
    membership = Membership.objects.filter(
        tenant_id=tenant_id, user_id=user_id, user__is_active=True
    ).first()
    if membership is None:
        raise ValidationError(_("Choose an active member of this workspace."))
    if membership.role != Membership.Role.ADMIN:
        from pyracast.plants.automation import disable_workspace_automation

        disable_workspace_automation(actor, tenant_id)
        Membership.objects.filter(tenant_id=tenant_id, role=Membership.Role.ADMIN).update(
            role=Membership.Role.VIEWER
        )
        membership.role = Membership.Role.ADMIN
        membership.save(update_fields=["role"])
        WorkspaceInvitation.objects.filter(
            tenant_id=tenant_id, accepted_at__isnull=True, expires_at__gt=timezone.now()
        ).update(expires_at=timezone.now())
        WorkspaceAdminChange.objects.create(
            tenant_id=tenant_id,
            actor=actor,
            administrator_id=user_id,
            previous_administrator_id=current_admin_id,
        )
    return membership
