from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpRequest

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.accounts.security import has_verified_mfa
from pyracast.errors import ErrorCode, ForbiddenError


def active_membership(actor: User, tenant_id: int) -> Membership | None:
    return Membership.objects.filter(
        tenant_id=tenant_id, user_id=actor.pk, user__is_active=True
    ).first()


def tenant_membership(
    actor: User, tenant_id: int, *, role: str | None = None, lock: bool = False
) -> Membership:
    membership = active_membership(actor, tenant_id)
    if membership is not None and lock:
        # Writers hold the tenant row so administrator reassignment cannot interleave; the
        # membership is read again under that lock, and outsiders never queue for it.
        Tenant.objects.select_for_update().get(pk=tenant_id)
        membership = active_membership(actor, tenant_id)
    if membership is None:
        raise ForbiddenError(ErrorCode.MEMBERSHIP_REQUIRED)
    if role == WorkspaceRole.ADMIN and membership.role != role:
        raise ForbiddenError(ErrorCode.WORKSPACE_ADMIN_REQUIRED)
    if role == WorkspaceRole.VIEWER and membership.role != role:
        raise ForbiddenError(ErrorCode.ADMIN_EDITS_DIRECTLY)
    return membership


def is_platform_admin(user: User | AnonymousUser) -> bool:
    return bool(user.is_active and user.is_staff and user.is_superuser)


def requires_admin_mfa(user: User | AnonymousUser) -> bool:
    return bool(user.is_staff or user.is_superuser)


def has_platform_admin_session(request: HttpRequest) -> bool:
    return is_platform_admin(request.user) and has_verified_mfa(request)


def require_platform_admin(actor: User) -> None:
    if not is_platform_admin(actor):
        raise ForbiddenError(ErrorCode.PLATFORM_ADMIN_REQUIRED)


def authorize_workspace_admin(actor: User, tenant_id: int, *, platform: bool = False) -> None:
    # Only the console passes `platform`; tenant API routes always require the membership.
    if not platform:
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        return
    require_platform_admin(actor)
    Tenant.objects.select_for_update().get(pk=tenant_id)
