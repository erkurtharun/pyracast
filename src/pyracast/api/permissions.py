from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request

from pyracast.accounts.models import Membership

if TYPE_CHECKING:
    from rest_framework.views import APIView


class WorkspaceWritePermission(BasePermission):
    message = gettext_lazy("Workspace administrator required.")

    def has_permission(self, request: Request, view: APIView) -> bool:
        tenant_id = view.kwargs.get("tenant_id")
        if request.method in SAFE_METHODS or tenant_id is None:
            return True
        if request.user.pk is None:
            return False
        return Membership.objects.filter(
            tenant_id=tenant_id,
            user_id=request.user.pk,
            user__is_active=True,
            role=Membership.Role.ADMIN,
        ).exists()
