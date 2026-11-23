from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from django.contrib.auth.models import User
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from pyracast.accounts.authorization import active_membership
from pyracast.accounts.models import WorkspaceRole
from pyracast.errors import ErrorCode, ForbiddenError

if TYPE_CHECKING:
    from rest_framework.views import APIView


class Access(StrEnum):
    PUBLIC = "public"
    ACCOUNT = "account"
    MEMBER = "member"
    ADMIN = "admin"


class Throttle(StrEnum):
    FORECAST = "forecast"
    DAYS = "days"
    CALIBRATION = "calibration"
    PRODUCTION_IMPORT = "production_import"
    SUBMISSION = "submission"
    INVITATION = "invitation"


@dataclass(frozen=True)
class Policy:
    access: Access
    throttle: Throttle | None = None


TENANT = "api/tenants/<int:tenant_id>/"
REGION = TENANT + "regions/<int:region_id>/"
PLANT = REGION + "plants/<int:plant_id>/"
PUBLIC = Policy(Access.PUBLIC)
ACCOUNT = Policy(Access.ACCOUNT)
MEMBER = Policy(Access.MEMBER)
ADMIN = Policy(Access.ADMIN)

# Keys are Django route patterns; a framework view whose route or method is missing is refused.
ROUTE_POLICIES: Mapping[str, Mapping[str, Policy]] = {
    "api/csp-reports/": {"POST": PUBLIC},
    "api/session/": {"GET": PUBLIC},
    "api/session/language/": {"POST": PUBLIC},
    "api/tenants/": {"GET": ACCOUNT},
    "api/invitations/accept/": {"POST": Policy(Access.ACCOUNT, Throttle.SUBMISSION)},
    "api/invitations/preview/": {"POST": Policy(Access.PUBLIC, Throttle.INVITATION)},
    TENANT + "change-requests/": {"GET": MEMBER},
    TENANT + "change-requests/<int:request_id>/": {"GET": MEMBER},
    TENANT + "change-requests/<int:request_id>/approve/": {"POST": ADMIN},
    TENANT + "change-requests/<int:request_id>/reject/": {"POST": ADMIN},
    TENANT + "change-requests/<int:request_id>/seen/": {"POST": MEMBER},
    TENANT + "change-requests/seen/": {"POST": MEMBER},
    TENANT + "change-requests/<int:request_id>/withdraw/": {"POST": MEMBER},
    TENANT + "invitations/": {"GET": ADMIN, "POST": ADMIN},
    TENANT + "invitations/<int:invitation_id>/": {"DELETE": ADMIN},
    TENANT + "invitations/<int:invitation_id>/resend/": {"POST": ADMIN},
    TENANT + "members/": {"GET": ADMIN},
    TENANT + "members/<int:user_id>/": {"DELETE": ADMIN},
    TENANT + "sites/": {"GET": MEMBER, "POST": Policy(Access.ADMIN, Throttle.SUBMISSION)},
    TENANT + "trash/regions/": {"GET": MEMBER},
    REGION: {"DELETE": ADMIN},
    REGION + "collection-schedule/": {"GET": MEMBER, "PUT": ADMIN},
    REGION + "forecast/": {"GET": Policy(Access.MEMBER, Throttle.FORECAST)},
    REGION + "location-change-requests/": {
        "GET": MEMBER,
        "POST": Policy(Access.ADMIN, Throttle.SUBMISSION),
    },
    REGION + "location-change-requests/<int:request_id>/withdraw/": {"POST": ADMIN},
    REGION + "plants/": {"GET": MEMBER, "POST": ADMIN},
    REGION + "reminders/": {"GET": MEMBER, "PUT": ADMIN},
    REGION + "reminders/preview/": {"POST": ADMIN},
    REGION + "restore/": {"POST": Policy(Access.ADMIN, Throttle.SUBMISSION)},
    REGION + "resubmit/": {"POST": Policy(Access.ADMIN, Throttle.SUBMISSION)},
    PLANT: {"PUT": ADMIN},
    PLANT + "calibration/automation/": {"GET": MEMBER, "PATCH": ADMIN},
    PLANT + "calibration/reset/": {"POST": ADMIN},
    PLANT + "calibrations/": {"GET": MEMBER, "POST": Policy(Access.ADMIN, Throttle.CALIBRATION)},
    PLANT + "calibrations/<int:calibration_id>/": {"GET": MEMBER},
    PLANT + "calibrations/<int:calibration_id>/activate/": {"POST": ADMIN},
    PLANT + "change-requests/incidents/": {"POST": Policy(Access.MEMBER, Throttle.SUBMISSION)},
    PLANT + "change-requests/production/": {
        "POST": Policy(Access.MEMBER, Throttle.PRODUCTION_IMPORT)
    },
    PLANT + "days/": {"GET": Policy(Access.MEMBER, Throttle.DAYS)},
    PLANT + "forecast/": {"GET": Policy(Access.MEMBER, Throttle.FORECAST)},
    PLANT + "outages/": {"GET": MEMBER, "POST": ADMIN},
    PLANT + "outages/<int:incident_id>/": {"PUT": ADMIN},
    PLANT + "outages/<int:incident_id>/cancel/": {"POST": ADMIN},
    PLANT + "outages/<int:incident_id>/resolve/": {"POST": ADMIN},
    PLANT + "performance/": {"GET": Policy(Access.MEMBER, Throttle.FORECAST)},
    PLANT + "production-source/": {"GET": MEMBER},
    PLANT + "production/": {"GET": MEMBER},
    PLANT + "production/changes/": {"POST": Policy(Access.ADMIN, Throttle.PRODUCTION_IMPORT)},
    PLANT + "production/preview/": {"POST": Policy(Access.ADMIN, Throttle.PRODUCTION_IMPORT)},
}


def route_policy(request: Request) -> Policy | None:
    """The policy of the request's route and method; raises 405 for a method the route lacks."""
    match = request.resolver_match
    methods = ROUTE_POLICIES.get(match.route) if match is not None else None
    if methods is None:
        return None
    method = cast(str, request.method)
    policy = methods.get("GET" if method == "HEAD" else method)
    if policy is None:
        raise MethodNotAllowed(method)
    return policy


class RoutePolicy(BasePermission):
    def has_permission(self, request: Request, view: APIView) -> bool:
        policy = route_policy(request)
        if policy is None or not (policy.access == Access.PUBLIC or request.user.is_authenticated):
            return False
        if policy.access in {Access.PUBLIC, Access.ACCOUNT}:
            return True
        membership = active_membership(cast(User, request.user), view.kwargs["tenant_id"])
        if membership is None:
            raise ForbiddenError(ErrorCode.MEMBERSHIP_REQUIRED)
        if policy.access == Access.ADMIN and membership.role != WorkspaceRole.ADMIN:
            raise ForbiddenError(ErrorCode.WORKSPACE_ADMIN_REQUIRED)
        return True
