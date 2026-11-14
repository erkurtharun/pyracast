from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from pyracast.accounts.models import Membership, Tenant, WorkspaceAdminChange, WorkspaceInvitation


def require_member(actor: User, tenant_id: int) -> None:
    if (
        not actor.is_active
        or not Membership.objects.filter(user=actor, tenant_id=tenant_id).exists()
    ):
        raise PermissionDenied("Tenant membership required.")


def require_admin(actor: User) -> None:
    if not actor.is_active or not getattr(actor, "is_superuser", False):
        raise PermissionDenied("Platform administrator required.")


def require_workspace_admin(actor: User, tenant_id: int) -> None:
    # The tenant lock serializes authorization with platform administrator reassignment.
    tenant = Tenant.objects.select_for_update().filter(pk=tenant_id).first()
    if (
        tenant is None
        or not Membership.objects.filter(
            user_id=actor.pk, user__is_active=True, tenant_id=tenant_id, role=Membership.Role.ADMIN
        ).exists()
    ):
        raise PermissionDenied(_("Workspace administrator required."))


@transaction.atomic
def reassign_workspace_admin(
    actor: User, tenant_id: int, user_id: int, *, expected_admin_id: int | None
) -> Membership:
    require_admin(actor)
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
