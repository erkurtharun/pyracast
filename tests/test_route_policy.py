import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, path
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.api.permissions import ROUTE_POLICIES, RoutePolicy
from pyracast.api.session import SessionView
from pyracast.plants.operations import create_plant
from pyracast.plants.parameters import PlantParameters
from tests.support.api import verified_admin
from tests.support.domain import make_system

OPENAPI = Path(__file__).resolve().parents[1] / "web" / "openapi.json"
TENANT = "api/tenants/<int:tenant_id>/"
REGION = TENANT + "regions/<int:region_id>/"
PLANT = REGION + "plants/<int:plant_id>/"
PUBLIC, ACCOUNT, MEMBER, ADMIN = "public", "account", "member", "admin"
FORECAST, CALIBRATION, IMPORT, SUBMISSION, INVITATION = (
    "forecast",
    "calibration",
    "production_import",
    "submission",
    "invitation",
)
POLICIES: dict[str, dict[str, tuple[str, str | None]]] = {
    "api/csp-reports/": {"POST": (PUBLIC, None)},
    "api/session/": {"GET": (PUBLIC, None)},
    "api/session/language/": {"POST": (PUBLIC, None)},
    "api/tenants/": {"GET": (ACCOUNT, None)},
    "api/invitations/accept/": {"POST": (ACCOUNT, SUBMISSION)},
    "api/invitations/preview/": {"POST": (PUBLIC, INVITATION)},
    TENANT + "change-requests/": {"GET": (MEMBER, None)},
    TENANT + "change-requests/<int:request_id>/": {"GET": (MEMBER, None)},
    TENANT + "change-requests/<int:request_id>/approve/": {"POST": (ADMIN, None)},
    TENANT + "change-requests/<int:request_id>/reject/": {"POST": (ADMIN, None)},
    TENANT + "change-requests/<int:request_id>/seen/": {"POST": (MEMBER, None)},
    TENANT + "change-requests/seen/": {"POST": (MEMBER, None)},
    TENANT + "change-requests/<int:request_id>/withdraw/": {"POST": (MEMBER, None)},
    TENANT + "invitations/": {"GET": (ADMIN, None), "POST": (ADMIN, None)},
    TENANT + "invitations/<int:invitation_id>/": {"DELETE": (ADMIN, None)},
    TENANT + "invitations/<int:invitation_id>/resend/": {"POST": (ADMIN, None)},
    TENANT + "members/": {"GET": (ADMIN, None)},
    TENANT + "members/<int:user_id>/": {"DELETE": (ADMIN, None)},
    TENANT + "sites/": {"GET": (MEMBER, None), "POST": (ADMIN, SUBMISSION)},
    TENANT + "trash/regions/": {"GET": (MEMBER, None)},
    REGION: {"DELETE": (ADMIN, None)},
    REGION + "collection-schedule/": {"GET": (MEMBER, None), "PUT": (ADMIN, None)},
    REGION + "forecast/": {"GET": (MEMBER, FORECAST)},
    REGION + "location-change-requests/": {
        "GET": (MEMBER, None),
        "POST": (ADMIN, SUBMISSION),
    },
    REGION + "location-change-requests/<int:request_id>/withdraw/": {"POST": (ADMIN, None)},
    REGION + "plants/": {"GET": (MEMBER, None), "POST": (ADMIN, None)},
    REGION + "reminders/": {"GET": (MEMBER, None), "PUT": (ADMIN, None)},
    REGION + "reminders/preview/": {"POST": (ADMIN, None)},
    REGION + "restore/": {"POST": (ADMIN, SUBMISSION)},
    REGION + "resubmit/": {"POST": (ADMIN, SUBMISSION)},
    PLANT: {"PUT": (ADMIN, None)},
    PLANT + "calibration/automation/": {"GET": (MEMBER, None), "PATCH": (ADMIN, None)},
    PLANT + "calibration/reset/": {"POST": (ADMIN, None)},
    PLANT + "calibrations/": {"GET": (MEMBER, None), "POST": (ADMIN, CALIBRATION)},
    PLANT + "calibrations/<int:calibration_id>/": {"GET": (MEMBER, None)},
    PLANT + "calibrations/<int:calibration_id>/activate/": {"POST": (ADMIN, None)},
    PLANT + "change-requests/incidents/": {"POST": (MEMBER, SUBMISSION)},
    PLANT + "change-requests/production/": {"POST": (MEMBER, IMPORT)},
    PLANT + "forecast/": {"GET": (MEMBER, FORECAST)},
    PLANT + "outages/": {"GET": (MEMBER, None), "POST": (ADMIN, None)},
    PLANT + "outages/<int:incident_id>/": {"PUT": (ADMIN, None)},
    PLANT + "outages/<int:incident_id>/cancel/": {"POST": (ADMIN, None)},
    PLANT + "outages/<int:incident_id>/resolve/": {"POST": (ADMIN, None)},
    PLANT + "performance/": {"GET": (MEMBER, FORECAST)},
    PLANT + "production-source/": {"GET": (MEMBER, None)},
    PLANT + "production/": {"GET": (MEMBER, None)},
    PLANT + "production/changes/": {"POST": (ADMIN, IMPORT)},
    PLANT + "production/preview/": {"POST": (ADMIN, IMPORT)},
}
ROLES = ("anonymous", "outsider", "platform_admin", "viewer", "workspace_admin")
SIGNED_OUT = {"anonymous": (401, "not_authenticated")}
NOT_A_MEMBER = {
    **SIGNED_OUT,
    "outsider": (403, "membership_required"),
    "platform_admin": (403, "membership_required"),
}
REFUSALS: dict[str, dict[str, tuple[int, str]]] = {
    PUBLIC: {},
    ACCOUNT: SIGNED_OUT,
    MEMBER: NOT_A_MEMBER,
    ADMIN: {**NOT_A_MEMBER, "viewer": (403, "workspace_admin_required")},
}
OK, NO_CONTENT = (200, None), (204, None)
INVALID, NOT_FOUND = (400, "invalid_request"), (404, "not_found")
NO_CONFIGURATION = (400, "configuration_unavailable")
# Admitted roles' answers with a real tenant, region and plant, other ids absent and body {}.
ANSWERS: dict[str, dict[str, tuple[int, str | None]]] = {
    "api/csp-reports/": {"POST": (415, "unsupported_media_type")},
    "api/session/": {"GET": OK},
    "api/session/language/": {"POST": INVALID},
    "api/tenants/": {"GET": OK},
    "api/invitations/accept/": {"POST": INVALID},
    "api/invitations/preview/": {"POST": (400, "invitation_unavailable")},
    TENANT + "change-requests/": {"GET": OK},
    TENANT + "change-requests/<int:request_id>/": {"GET": NOT_FOUND},
    TENANT + "change-requests/<int:request_id>/approve/": {"POST": INVALID},
    TENANT + "change-requests/<int:request_id>/reject/": {"POST": INVALID},
    TENANT + "change-requests/<int:request_id>/seen/": {"POST": NOT_FOUND},
    TENANT + "change-requests/seen/": {"POST": OK},
    TENANT + "change-requests/<int:request_id>/withdraw/": {"POST": NOT_FOUND},
    TENANT + "invitations/": {"GET": OK, "POST": INVALID},
    TENANT + "invitations/<int:invitation_id>/": {"DELETE": (400, "invitation_unavailable")},
    TENANT + "invitations/<int:invitation_id>/resend/": {"POST": (400, "invitation_unavailable")},
    TENANT + "members/": {"GET": OK},
    TENANT + "members/<int:user_id>/": {"DELETE": NO_CONTENT},
    TENANT + "sites/": {"GET": OK, "POST": INVALID},
    TENANT + "trash/regions/": {"GET": OK},
    REGION: {"DELETE": NO_CONTENT},
    REGION + "collection-schedule/": {"GET": OK, "PUT": INVALID},
    REGION + "forecast/": {"GET": OK},
    REGION + "location-change-requests/": {"GET": OK, "POST": INVALID},
    REGION + "location-change-requests/<int:request_id>/withdraw/": {"POST": NOT_FOUND},
    REGION + "plants/": {"GET": OK, "POST": INVALID},
    REGION + "reminders/": {"GET": OK, "PUT": INVALID},
    REGION + "reminders/preview/": {"POST": INVALID},
    REGION + "restore/": {"POST": OK},
    REGION + "resubmit/": {"POST": INVALID},
    PLANT: {"PUT": INVALID},
    PLANT + "calibration/automation/": {"GET": OK, "PATCH": INVALID},
    PLANT + "calibration/reset/": {"POST": NO_CONFIGURATION},
    PLANT + "calibrations/": {"GET": OK, "POST": INVALID},
    PLANT + "calibrations/<int:calibration_id>/": {"GET": NOT_FOUND},
    PLANT + "calibrations/<int:calibration_id>/activate/": {"POST": NO_CONFIGURATION},
    PLANT + "change-requests/incidents/": {"POST": INVALID},
    PLANT + "change-requests/production/": {"POST": INVALID},
    PLANT + "forecast/": {"GET": OK},
    PLANT + "outages/": {"GET": OK, "POST": INVALID},
    PLANT + "outages/<int:incident_id>/": {"PUT": INVALID},
    PLANT + "outages/<int:incident_id>/cancel/": {"POST": INVALID},
    PLANT + "outages/<int:incident_id>/resolve/": {"POST": INVALID},
    PLANT + "performance/": {"GET": OK},
    PLANT + "production-source/": {"GET": OK},
    PLANT + "production/": {"GET": OK},
    PLANT + "production/changes/": {"POST": INVALID},
    PLANT + "production/preview/": {"POST": INVALID},
}


class UnlistedView(APIView):
    def get(self, request: Request) -> Response:
        return Response({"served": True})


urlpatterns = [
    path("api/unlisted/", UnlistedView.as_view()),
    path("api/unlisted/session/", SessionView.as_view()),
]


def _walk(
    patterns: list[URLPattern | URLResolver], prefix: str = ""
) -> Iterator[tuple[str, Callable[..., Any]]]:
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _walk(pattern.url_patterns, prefix + str(pattern.pattern))
        else:
            yield prefix + str(pattern.pattern), pattern.callback


def framework_views() -> dict[str, type[APIView]]:
    return {
        route: view
        for route, callback in _walk(get_resolver().url_patterns)
        if isinstance(view := getattr(callback, "cls", None), type) and issubclass(view, APIView)
    }


def test_every_framework_route_and_method_has_exactly_one_policy() -> None:
    table = ROUTE_POLICIES
    expected = {
        route: {
            method.upper()
            for method in view.http_method_names
            if method not in {"head", "options"} and hasattr(view, method)
        }
        for route, view in framework_views().items()
    }
    assert {route: set(methods) for route, methods in table.items()} == expected


def test_every_framework_view_takes_its_permission_from_the_route_policy() -> None:
    views = framework_views()
    assert {
        route: [type(permission) for permission in view().get_permissions()]
        for route, view in views.items()
    } == {route: [RoutePolicy] for route in views}


def test_the_policy_table_is_the_reviewed_access_matrix() -> None:
    table = ROUTE_POLICIES
    assert {
        route: {method: (rule.access, rule.throttle) for method, rule in methods.items()}
        for route, methods in table.items()
    } == POLICIES


def client_for(role: str, tenant_id: int, admin: User) -> Client:
    if role == "platform_admin":
        return verified_admin()[0]
    client = Client(raise_request_exception=False)
    if role == "workspace_admin":
        client.force_login(admin)
    elif role in {"outsider", "viewer"}:
        user = User.objects.create_user(username=role)
        if role == "viewer":
            Membership.objects.create(user=user, tenant_id=tenant_id, role=WorkspaceRole.VIEWER)
        client.force_login(user)
    return client


def concrete(route: str, ids: dict[str, int]) -> str:
    for name, value in ids.items():
        route = route.replace(f"<int:{name}>", str(value))
    return "/" + route


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("route", "method"),
    [(route, method) for route, methods in POLICIES.items() for method in methods],
)
def test_each_role_meets_the_route_policy(route: str, method: str) -> None:
    system = make_system()
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        PlantParameters(name="Plant", dc_capacity_mw=10),
    )
    access = POLICIES[route][method][0]
    present = {"tenant_id": system.tenant.pk, "region_id": system.region.pk, "plant_id": plant.pk}
    absent = dict.fromkeys(
        ("request_id", "invitation_id", "user_id", "calibration_id", "incident_id"), 999999
    )
    url = concrete(route, {**present, **absent})
    if route.endswith("/forecast/"):
        url += f"?date={timezone.localdate(timezone=ZoneInfo(system.region.timezone))}"
    outcomes = {}
    for role in ROLES:
        client = client_for(role, system.tenant.pk, system.member)
        response = client.generic(method, url, b"{}", content_type="application/json")
        code = response.json()["code"] if response.status_code >= 400 else None
        outcomes[role] = (response.status_code, code)
    assert outcomes == {role: REFUSALS[access].get(role, ANSWERS[route][method]) for role in ROLES}


@pytest.mark.django_db
@pytest.mark.urls("tests.test_route_policy")
@pytest.mark.parametrize("unlisted", ["/api/unlisted/", "/api/unlisted/session/"])
def test_a_route_without_a_policy_is_refused(unlisted: str) -> None:
    assert Client().get(unlisted).status_code == 401
    client = Client()
    client.force_login(User.objects.create_user(username="person"))
    response = client.get(unlisted)
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@pytest.mark.django_db
def test_a_method_without_a_policy_answers_405_before_the_view_runs() -> None:
    system = make_system()
    client = Client()
    client.force_login(system.member)
    members = f"/api/tenants/{system.tenant.pk}/members/"
    response = client.options(members)
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"
    assert client.head(members).status_code == 200
    outsider = Client()
    outsider.force_login(User.objects.create_user(username="outsider"))
    assert outsider.head(members).status_code == 403


def test_operations_document_the_refusals_their_policy_allows() -> None:
    schema = json.loads(OPENAPI.read_text(encoding="utf-8"))
    documented = {}
    for route, methods in schema["paths"].items():
        for method, operation in methods.items():
            documented[(route, method)] = sorted(
                status for status in operation["responses"] if status.startswith(("4", "5"))
            )
    error = {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}
    conflicts = [
        {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}}
        for name in ("IncidentConflict", "PlantConflict")
    ]
    outage_create = "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/"
    location = {"application/json": {"schema": {"$ref": "#/components/schemas/LocationRefusal"}}}
    located = {
        ("/api/tenants/{tenant_id}/sites/", "post"),
        ("/api/tenants/{tenant_id}/regions/{region_id}/location-change-requests/", "post"),
    }
    assert schema["components"]["schemas"]["LocationRefusal"]["allOf"][0] == {
        "$ref": "#/components/schemas/Error"
    }
    for name in ("IncidentConflict", "PlantConflict"):
        assert schema["components"]["schemas"][name]["properties"].keys() == {
            "code",
            "message",
            "current",
        }
    assert documented[("/api/session/", "get")] == []
    assert documented[("/api/session/language/", "post")] == ["400", "403", "415"]
    assert documented[("/api/tenants/", "get")] == ["401", "404"]
    assert documented[("/api/tenants/{tenant_id}/members/", "get")] == ["401", "403"]
    assert documented[("/api/tenants/{tenant_id}/members/{user_id}/", "delete")] == [
        "401",
        "403",
        "404",
    ]
    assert documented[("/api/tenants/{tenant_id}/sites/", "post")] == [
        "400",
        "401",
        "403",
        "415",
        "429",
    ]
    forecast = schema["paths"]["/api/tenants/{tenant_id}/regions/{region_id}/forecast/"]["get"]
    refusals = ("400", "401", "403", "404", "429")
    assert sorted(forecast["responses"]) == ["200", *refusals, "default"]
    assert all(forecast["responses"][status]["content"] == error for status in refusals)
    for route, methods in schema["paths"].items():
        pattern = re.sub(r"\{(\w+)\}", r"<int:\1>", route.removeprefix("/"))
        for method, operation in methods.items():
            access, throttle = POLICIES[pattern][method.upper()]
            responses = operation["responses"]
            assert ("401" in responses) == (access != PUBLIC)
            assert ("403" in responses) == (method != "get" or access in {MEMBER, ADMIN})
            assert ("429" in responses) == (throttle is not None)
            if "requestBody" in operation:
                assert "415" in responses
            for status, response in responses.items():
                if status.startswith(("4", "5")) or status == "default":
                    if status == "409" and (route, method) == (outage_create, "post"):
                        assert response["content"] == error
                    elif status == "409":
                        assert response["content"] in conflicts
                    elif status == "400" and (route, method) in located:
                        assert response["content"] == location
                    else:
                        assert response["content"] == error


def test_a_request_that_bypassed_the_url_resolver_is_refused() -> None:
    request = Request(APIRequestFactory().get("/api/session/"))
    assert not RoutePolicy().has_permission(request, APIView())
