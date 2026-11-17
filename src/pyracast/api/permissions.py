from __future__ import annotations

from typing import TYPE_CHECKING, cast

from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request

from pyracast.accounts.authorization import active_membership
from pyracast.accounts.models import WorkspaceRole

if TYPE_CHECKING:
    from rest_framework.views import APIView


class WorkspaceWritePermission(BasePermission):
    message = gettext_lazy("Workspace administrator required.")

    def has_permission(self, request: Request, view: APIView) -> bool:
        tenant_id = view.kwargs.get("tenant_id")
        if request.method in SAFE_METHODS or tenant_id is None:
            return True
        membership = active_membership(cast(User, request.user), tenant_id)
        return membership is not None and membership.role == WorkspaceRole.ADMIN


class WorkspaceMemberWritePermission(BasePermission):
    def has_permission(self, request: Request, view: APIView) -> bool:
        tenant_id = view.kwargs.get("tenant_id")
        return active_membership(cast(User, request.user), tenant_id) is not None
