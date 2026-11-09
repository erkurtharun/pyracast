from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied

from pyracast.accounts.models import Membership


def require_member(actor: User, tenant_id: int) -> None:
    if (
        not actor.is_active
        or not Membership.objects.filter(user=actor, tenant_id=tenant_id).exists()
    ):
        raise PermissionDenied("Tenant membership required.")


def require_admin(actor: User) -> None:
    if not actor.is_active or not getattr(actor, "is_superuser", False):
        raise PermissionDenied("Platform administrator required.")
