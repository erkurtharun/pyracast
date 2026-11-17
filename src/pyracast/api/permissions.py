from __future__ import annotations

from typing import TYPE_CHECKING, cast

from django.contrib.auth.models import User
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request

from pyracast.accounts.authorization import active_membership
from pyracast.accounts.models import Membership
from pyracast.errors import ErrorCode

if TYPE_CHECKING:
    from rest_framework.views import APIView


class WorkspaceWritePermission(BasePermission):
    code = ErrorCode.WORKSPACE_ADMIN_REQUIRED.value

    def has_permission(self, request: Request, view: APIView) -> bool:
        tenant_id = view.kwargs.get("tenant_id")
        if request.method in SAFE_METHODS or tenant_id is None:
            return True
        membership = active_membership(cast(User, request.user), tenant_id)
        return membership is not None and membership.role == Membership.Role.ADMIN


class WorkspaceMemberWritePermission(BasePermission):
    code = ErrorCode.MEMBERSHIP_REQUIRED.value

    def has_permission(self, request: Request, view: APIView) -> bool:
        tenant_id = view.kwargs.get("tenant_id")
        return active_membership(cast(User, request.user), tenant_id) is not None
