from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from django import forms
from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.test import Client
from django.urls import NoReverseMatch, URLPattern, URLResolver, get_resolver, reverse

from pyracast.accounts.headless import ManageEmailView
from pyracast.connections.models import ProviderAccount
from pyracast.notifications.models import NotificationSettings
from pyracast.platform_admin.forms import ProviderSetupForm
from pyracast.platform_admin.site import site
from tests.support.api import verified_admin
from tests.support.domain import make_system

TENANT = "api/tenants/<int:tenant_id>/"
REGION = TENANT + "regions/<int:region_id>/"
PLANT = REGION + "plants/<int:plant_id>/"
API_ROUTES = sorted(
    [
        ("api/accounts/google/login/callback/", ()),
        ("api/accounts/microsoft/login/callback/", ()),
        ("api/csp-reports/", ("POST",)),
        ("api/invitations/accept/", ("POST",)),
        ("api/invitations/preview/", ("POST",)),
        ("api/session/", ("GET",)),
        ("api/session/language/", ("POST",)),
        ("api/tenants/", ("GET",)),
        (TENANT + "change-requests/", ("GET",)),
        (TENANT + "change-requests/<int:request_id>/", ("GET",)),
        (TENANT + "change-requests/<int:request_id>/approve/", ("POST",)),
        (TENANT + "change-requests/<int:request_id>/reject/", ("POST",)),
        (TENANT + "change-requests/<int:request_id>/seen/", ("POST",)),
        (TENANT + "change-requests/seen/", ("POST",)),
        (TENANT + "change-requests/<int:request_id>/withdraw/", ("POST",)),
        (TENANT + "invitations/", ("GET", "POST")),
        (TENANT + "invitations/<int:invitation_id>/", ("DELETE",)),
        (TENANT + "invitations/<int:invitation_id>/resend/", ("POST",)),
        (TENANT + "members/", ("GET",)),
        (TENANT + "members/<int:user_id>/", ("DELETE",)),
        (TENANT + "sites/", ("GET", "POST")),
        (TENANT + "trash/regions/", ("GET",)),
        (REGION, ("DELETE",)),
        (REGION + "collection-schedule/", ("GET", "PUT")),
        (REGION + "forecast/", ("GET",)),
        (REGION + "location-change-requests/", ("GET", "POST")),
        (REGION + "location-change-requests/<int:request_id>/withdraw/", ("POST",)),
        (REGION + "plants/", ("GET", "POST")),
        (REGION + "reminders/", ("GET", "PUT")),
        (REGION + "reminders/preview/", ("POST",)),
        (REGION + "restore/", ("POST",)),
        (REGION + "resubmit/", ("POST",)),
        (PLANT, ("PUT",)),
        (PLANT + "calibration/automation/", ("GET", "PATCH")),
        (PLANT + "calibration/reset/", ("POST",)),
        (PLANT + "calibrations/", ("GET", "POST")),
        (PLANT + "calibrations/<int:calibration_id>/", ("GET",)),
        (PLANT + "calibrations/<int:calibration_id>/activate/", ("POST",)),
        (PLANT + "change-requests/incidents/", ("POST",)),
        (PLANT + "change-requests/production/", ("POST",)),
        (PLANT + "forecast/", ("GET",)),
        (PLANT + "outages/", ("GET", "POST")),
        (PLANT + "outages/<int:incident_id>/", ("PUT",)),
        (PLANT + "outages/<int:incident_id>/cancel/", ("POST",)),
        (PLANT + "outages/<int:incident_id>/resolve/", ("POST",)),
        (PLANT + "performance/", ("GET",)),
        (PLANT + "production-source/", ("GET",)),
        (PLANT + "production/", ("GET",)),
        (PLANT + "production/changes/", ("POST",)),
        (PLANT + "production/preview/", ("POST",)),
    ]
)
AUTH = "api/auth/browser/v1/"
AUTH_ROUTES = sorted(
    AUTH + route
    for route in (
        "account/authenticators",
        "account/authenticators/recovery-codes",
        "account/authenticators/totp",
        "account/email",
        "account/password/change",
        "account/providers",
        "auth/2fa/authenticate",
        "auth/2fa/reauthenticate",
        "auth/email/verify",
        "auth/email/verify/resend",
        "auth/login",
        "auth/password/request",
        "auth/password/reset",
        "auth/provider/complete",
        "auth/provider/redirect",
        "auth/provider/signup",
        "auth/reauthenticate",
        "auth/reauthenticate/code/confirm",
        "auth/reauthenticate/code/request",
        "auth/session",
        "auth/sessions",
        "auth/signup",
        "config",
    )
)
CONSOLE_ROUTES = sorted(
    [
        "admin/",
        "admin/(?P<url>.*)$",
        "admin/^(?P<app_label>notifications|forecasts|regions|plants|connections|accounts)/$",
        "admin/confirm/",
        "admin/jsi18n/",
        "admin/language/",
        "admin/login/",
        "admin/logout/",
        "admin/operations/",
        "admin/password_change/",
        "admin/plants/",
        "admin/plants/<int:region_id>/",
        "admin/plants/<int:region_id>/decision/",
        "admin/plants/<int:region_id>/details/",
        "admin/plants/<int:region_id>/historical-import/",
        "admin/provider-accounts/",
        "admin/provider-accounts/<int:account_id>/",
        "admin/provider-accounts/<int:account_id>/delay/",
        "admin/provider-accounts/<int:account_id>/key/",
        "admin/r/<path:content_type_id>/<path:object_id>/",
        "admin/settings/",
        "admin/workspaces/",
        "admin/workspaces/new/",
        "admin/workspaces/<int:tenant_id>/",
        "admin/workspaces/<int:tenant_id>/administrator/",
        "admin/workspaces/<int:tenant_id>/invitations/new/",
        "admin/workspaces/<int:tenant_id>/invitations/<int:invitation_id>/reissue/",
        "admin/workspaces/<int:tenant_id>/invitations/<int:invitation_id>/revocation/",
        "admin/workspaces/<int:tenant_id>/members/<int:user_id>/removal/",
        "admin/workspaces/<int:tenant_id>/name/",
        "admin/workspaces/<int:tenant_id>/plants/new/",
    ]
)
RECORD_BROWSER_MODELS = {
    "accounts.tenant",
    "accounts.workspaceadminchange",
    "accounts.workspacememberremoval",
    "connections.sitebinding",
    "forecasts.collectionjob",
    "forecasts.dispatcherstatus",
    "forecasts.forecastresult",
    "forecasts.requestattempt",
    "notifications.notificationdelivery",
    "notifications.notificationsettings",
    "notifications.outboundmail",
    "plants.calibrationactivation",
    "plants.calibrationrun",
    "plants.plant",
    "plants.plantconfiguration",
    "plants.plantevent",
    "plants.productionobservation",
    "regions.auditevent",
    "regions.region",
    "regions.regionlocationchangerequest",
}
RECORD_BROWSER_ROUTES = ("", "<path:object_id>/history/", "<path:object_id>/change/")


def _walk(
    patterns: list[URLPattern | URLResolver], prefix: str = ""
) -> Iterator[tuple[str, Callable[..., Any]]]:
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _walk(pattern.url_patterns, prefix + str(pattern.pattern))
        else:
            yield prefix + str(pattern.pattern), pattern.callback


def _methods(callback: Callable[..., Any]) -> tuple[str, ...]:
    view = getattr(callback, "cls", None)
    if view is None:
        return ()
    return tuple(
        sorted(
            method.upper()
            for method in view.http_method_names
            if method not in {"head", "options"} and hasattr(view, method)
        )
    )


def _routes(prefix: str) -> list[tuple[str, Callable[..., Any]]]:
    return [
        (route, callback)
        for route, callback in _walk(get_resolver().url_patterns)
        if route.startswith(prefix)
    ]


def test_api_routes_equal_the_product_surface() -> None:
    routed = sorted(
        (route, _methods(callback))
        for route, callback in _routes("api/")
        if not route.startswith("api/auth/")
    )
    assert routed == API_ROUTES


def test_sign_in_routes_are_the_listed_allauth_views() -> None:
    assert sorted(route for route, _ in _routes("api/auth/")) == AUTH_ROUTES


def test_email_management_is_routed_once_to_the_project_view() -> None:
    (callback,) = [
        callback for route, callback in _routes("api/auth/") if route == AUTH + "account/email"
    ]
    assert getattr(callback, "view_class", None) is ManageEmailView


def test_console_routes_equal_the_operator_workspace() -> None:
    model_routes = {
        f"admin/{label.replace('.', '/')}/{suffix}"
        for label in RECORD_BROWSER_MODELS
        for suffix in RECORD_BROWSER_ROUTES
    }
    routed = [route for route, _ in _routes("admin/")]
    assert len(routed) == len(set(routed))
    assert sorted(set(routed) - model_routes) == CONSOLE_ROUTES
    assert model_routes <= set(routed)
    assert {model._meta.label_lower for model in site._registry} == RECORD_BROWSER_MODELS


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("method", "route"),
    [
        ("get", "account/phone"),
        ("post", "auth/code/confirm"),
        ("post", "auth/phone/verify"),
        ("post", "auth/phone/verify/resend"),
        ("post", "auth/reauthenticate/code/resend"),
        ("get", "auth/provider/token"),
        ("post", "auth/provider/token"),
        ("put", "auth/provider/token"),
    ],
)
def test_unlisted_allauth_routes_answer_404(method: str, route: str) -> None:
    client = Client(raise_request_exception=False)
    client.force_login(User.objects.create_user(username="member", email="member@example.com"))
    response = getattr(client, method)("/" + AUTH + route, {}, content_type="application/json")
    assert response.status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", "/api/schema/"),
        ("post", "/api/platform/accounts/"),
        ("post", "/api/platform/accounts/{account}/rotate-secret/"),
        ("post", "/api/platform/regions/{region}/bindings/"),
        ("post", "/api/platform/bindings/{binding}/validate/"),
        ("post", "/api/platform/regions/{region}/approve/"),
        ("post", "/api/platform/regions/{region}/transition/"),
        ("get", "/api/tenants/{tenant}/regions/"),
        ("post", "/api/tenants/{tenant}/regions/"),
        ("get", "/api/tenants/{tenant}/sites/{region}/"),
        ("get", "/api/tenants/{tenant}/regions/{region}/plants/{plant}/observations/"),
        ("post", "/api/tenants/{tenant}/regions/{region}/plants/{plant}/observations/"),
        ("get", "/api/tenants/{tenant}/regions/{region}/plants/{plant}/reported-production/"),
    ],
)
def test_routes_without_a_product_caller_answer_404(method: str, suffix: str) -> None:
    system = make_system()
    client, _ = verified_admin()
    path = suffix.format(
        account=system.account.pk,
        region=system.region.pk,
        binding=system.binding.pk,
        tenant=system.tenant.pk,
        plant=1,
    )
    response = getattr(client, method)(path, {}, content_type="application/json")
    assert response.status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", "regions/{region}/"),
        ("put", "regions/{region}/"),
        ("patch", "regions/{region}/"),
        ("get", "regions/{region}/plants/{plant}/"),
        ("put", "regions/{region}/plants/{plant}/production-source/"),
        ("delete", "regions/{region}/plants/{plant}/production-source/"),
    ],
)
def test_methods_without_a_product_caller_answer_405(method: str, suffix: str) -> None:
    system = make_system()
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/" + suffix.format(region=system.region.pk, plant=1)
    response = getattr(client, method)(path, {"name": "Renamed"}, content_type="application/json")
    assert response.status_code == 405
    system.region.refresh_from_db()
    assert system.region.name == "Site"


@pytest.mark.django_db
def test_form_encoded_body_answers_415() -> None:
    client = Client()
    response = client.post("/api/session/language/", {"language": "tr"})
    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"


@pytest.mark.django_db
def test_permission_refusal_precedes_body_parsing() -> None:
    system = make_system()
    outsider = Client()
    outsider.force_login(User.objects.create_user(username="outsider"))
    response = outsider.post(f"/api/tenants/{system.tenant.pk}/sites/", {"name": "Form"})
    assert response.status_code == 403


@pytest.mark.django_db
def test_no_http_route_accepts_the_fixture_provider() -> None:
    client, _ = verified_admin()
    response = client.post(
        "/api/platform/accounts/",
        {"provider": "fixture", "external_account_id": "fixture-http", "api_key": ""},
        content_type="application/json",
    )
    assert response.status_code == 404
    assert not ProviderAccount.objects.filter(external_account_id="fixture-http").exists()
    make_system()
    field = cast(forms.ChoiceField, ProviderSetupForm().fields["existing_account"])
    assert [value for value, _ in cast(list[tuple[str, str]], field.choices) if value] == []


@pytest.mark.django_db
def test_console_offers_no_bulk_or_raw_write_actions() -> None:
    system = make_system()
    client, _ = verified_admin()
    for model_admin in site._registry.values():
        request = client.get("/admin/").wsgi_request
        assert model_admin.get_actions(request) == {}
    settings_row, _ = NotificationSettings.objects.update_or_create(
        pk=1, defaults={"max_recipients": 5}
    )
    record = f"/admin/notifications/notificationsettings/{settings_row.pk}/"
    assert client.post(record + "change/", {"max_recipients": 9}).status_code == 403
    assert client.post(record + "delete/", {"post": "yes"}).status_code == 404
    add = "/admin/notifications/notificationsettings/add/"
    assert client.post(add, {"max_recipients": 9}).status_code == 404
    settings_row.refresh_from_db()
    assert settings_row.max_recipients == 5
    region_record = f"/admin/regions/region/{system.region.pk}/"
    assert client.get(region_record).status_code == 404
    assert client.get(region_record + "setup/").status_code == 404
    assert client.get(region_record + "change/").status_code == 200
    with pytest.raises(NoReverseMatch):
        reverse("admin:regions_region_setup", args=(system.region.pk,))
    assert client.get("/admin/autocomplete/").status_code == 404
    assert client.get("/admin/password_change/done/").status_code == 404
    assert django_admin.site._registry == {}
