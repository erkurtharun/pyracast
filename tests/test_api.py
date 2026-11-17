import logging
import re
from datetime import date
from time import time

import pytest
from allauth.account.internal.flows.login import record_authentication
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import (
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
)
from django.conf import LazySettings
from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, RequestFactory
from django.utils import timezone
from rest_framework.exceptions import ValidationError as APIValidationError

from pyracast.accounts.models import Membership, Tenant
from pyracast.admin import ReadOnlyAdmin, RegionAdmin, site
from pyracast.api.errors import exception_handler
from pyracast.api.serializers import RegionInput
from pyracast.api.views import TenantsView
from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import SiteBinding
from pyracast.connections.operations import QuotaExceededError
from pyracast.forecasts.jobs import collect
from pyracast.forecasts.models import CollectionJob, ForecastResult
from pyracast.providers import FixtureProvider
from pyracast.regions.models import Region

pytestmark = pytest.mark.django_db


@pytest.fixture
def customer() -> tuple[Client, User, Tenant]:
    user = User.objects.create_user(username="customer")
    tenant = Tenant.objects.create(name="Customer")
    Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.ADMIN)
    client = Client()
    client.force_login(user)
    return client, user, tenant


def verified_admin(*, enforce_csrf_checks: bool = False) -> tuple[Client, User]:
    user = User.objects.create_user(username="operator", is_superuser=True, is_staff=True)
    authenticator = Authenticator.objects.create(user=user, type="totp", data={})
    client = Client(enforce_csrf_checks=enforce_csrf_checks)
    client.force_login(user)
    request = RequestFactory().get("/admin/")
    request.user = user
    request.session = client.session
    record_authentication(request, user, method="mfa", type="totp", id=authenticator.pk)
    request.session.save()
    return client, user


def region_input() -> dict[str, object]:
    return {"name": "Reference", "latitude": 39.9, "longitude": 32.8, "timezone": "Europe/Istanbul"}


def create_region(client: Client, tenant: Tenant) -> int:
    response = client.post(
        f"/api/tenants/{tenant.pk}/regions/", region_input(), content_type="application/json"
    )
    assert response.status_code == 201
    return int(response.json()["id"])


def setup_binding(client: Client, region_id: int) -> int:
    account = client.post(
        "/api/platform/accounts/",
        {
            "provider": "fixture",
            "external_account_id": "canonical",
            "api_key": "",
        },
        content_type="application/json",
    )
    assert account.status_code == 201
    assert "secret" not in account.content.decode()
    response = client.post(
        f"/api/platform/regions/{region_id}/bindings/",
        {
            "provider_account_id": account.json()["id"],
            "site_id": "reference",
            "capacity_kw": 1,
            "capacity_semantics_verified": True,
            "installation_date": "2020-01-01",
        },
        content_type="application/json",
    )
    assert response.status_code == 201
    return int(response.json()["id"])


# The region forecast opens its own read-only snapshot transaction.
@pytest.mark.django_db(transaction=True)
def test_tenant_isolation_and_read_only_ownership(customer: tuple[Client, User, Tenant]) -> None:
    client, user, tenant = customer
    region_id = create_region(client, tenant)
    base = f"/api/tenants/{tenant.pk}/regions/"
    assert client.get(base).json()["count"] == 1
    assert client.get(f"{base}{region_id}/").json()["status"] == "pending"
    other = Tenant.objects.create(name="Other")
    other_user = User.objects.create_user(username="other")
    Membership.objects.create(user=other_user, tenant=other, role=Membership.Role.ADMIN)
    client.force_login(other_user)
    assert client.get(base).status_code == 403
    assert client.get(f"{base}{region_id}/").status_code == 403
    assert client.post(base, region_input(), content_type="application/json").status_code == 403
    own = f"/api/tenants/{other.pk}/regions/{region_id}/"
    assert client.get(own).status_code == 404
    assert client.put(own, region_input(), content_type="application/json").status_code == 404
    assert client.get(f"{own}forecast/?date={date.today()}").status_code == 404
    client.force_login(user)
    assert (
        client.post(
            base, {**region_input(), "status": "approved"}, content_type="application/json"
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"{base}{region_id}/", {"name": "Renamed"}, content_type="application/json"
        ).json()["revision"]
        == 1
    )
    assert (
        client.post(
            base, {**region_input(), "timezone": "not-a-zone"}, content_type="application/json"
        ).status_code
        == 400
    )
    assert Client().get(base).status_code == 403
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(user)
    assert csrf.post(base, region_input(), content_type="application/json").status_code == 403
    assert (
        csrf.post(
            "/api/auth/browser/v1/auth/login",
            {"email": "customer@example.com", "password": "test"},
            content_type="application/json",
        ).status_code
        == 403
    )


# The region forecast opens its own read-only snapshot transaction.
@pytest.mark.django_db(transaction=True)
def test_fixture_workflow_and_freshness(customer: tuple[Client, User, Tenant]) -> None:
    client, _, tenant = customer
    region_id = create_region(client, tenant)
    admin_client, _ = verified_admin()
    binding_id = setup_binding(admin_client, region_id)
    approval = f"/api/platform/regions/{region_id}/approve/"
    assert admin_client.post(approval, {"binding_id": binding_id}).status_code == 400
    assert admin_client.post(f"/api/platform/bindings/{binding_id}/validate/").status_code == 200
    approved = admin_client.post(approval, {"binding_id": binding_id})
    assert approved.status_code == 200
    job = CollectionJob.objects.get(pk=approved.json()["id"])
    path = f"/api/tenants/{tenant.pk}/regions/{region_id}/forecast/?date={job.target_date}"
    assert client.get(path).json()["freshness"] == "missing"
    assert collect(job.pk, FixtureProvider(), calculate, timezone.now())
    response = client.get(path)
    assert response.status_code == 200
    data = response.json()
    assert data["freshness"] == "fresh"
    assert data["issued_at"] is None
    assert data["result"]["energy_kwh_m2"] == pytest.approx(7.2)
    assert data["result"]["complete"] is True
    assert "unvalidated" in data["result"]["quality_flags"]
    assert not collect(job.pk, FixtureProvider(), calculate, timezone.now())
    assert ForecastResult.objects.count() == 1
    assert (
        admin_client.post(
            f"/api/platform/regions/{region_id}/transition/", {"status": "suspended"}
        ).status_code
        == 200
    )
    assert client.get(path).json()["freshness"] == "stale"
    region = Region.objects.get(pk=region_id)
    region.timezone = "America/New_York"
    region.save(update_fields=["timezone"])
    assert client.get(path).json()["timezone"] == "Europe/Istanbul"
    assert client.get(path).json()["scheduled_slot"] is not None
    assert isinstance(client.get(path).json()["late"], bool)
    assert client.get(path.replace(str(job.target_date), "1900-01-01")).status_code == 400
    assert client.get(path.replace(str(job.target_date), "invalid")).status_code == 400
    assert admin_client.get("/admin/").status_code == 200
    assert admin_client.get("/admin/regions/region/").status_code == 200
    schema = client.get("/api/schema/?format=json")
    assert schema.status_code == 200
    assert "/api/platform/accounts/" not in schema.json()["paths"]


def test_mfa_and_admin_mutation_boundaries(customer: tuple[Client, User, Tenant]) -> None:
    client, user, tenant = customer
    region_id = create_region(client, tenant)
    assert client.post("/api/platform/accounts/", {}).status_code == 403
    user.is_staff = True
    user.is_superuser = True
    user.save()
    assert client.post("/api/platform/accounts/", {}).status_code == 403
    assert client.get("/admin/").status_code == 302
    admin_client, admin_user = verified_admin()
    response = admin_client.post(
        "/admin/regions/region/",
        {
            "action": "approve",
            "_selected_action": [str(region_id)],
        },
        follow=True,
    )
    assert response.status_code == 200
    assert b"Validate the current binding" in response.content
    binding_id = setup_binding(admin_client, region_id)
    assert admin_client.post(f"/api/platform/bindings/{binding_id}/validate/").status_code == 200
    request = RequestFactory().post("/admin/")
    request.user = admin_user
    queryset = Region.objects.filter(pk=region_id)
    operator = RegionAdmin(Region, site)
    operator.approve(request, queryset)
    operator.reject(request, queryset)
    operator.suspend(request, queryset)
    operator.approve(request, Region.objects.none())
    operator.reject(request, Region.objects.none())
    operator.suspend(request, Region.objects.none())
    read_only = ReadOnlyAdmin(Region, site)
    assert not read_only.has_add_permission(request)
    assert not read_only.has_change_permission(request)
    assert not read_only.has_delete_permission(request)
    request.user = AnonymousUser()
    assert not site.has_permission(request)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ObjectDoesNotExist(), 404),
        (ValidationError("invalid"), 400),
        (QuotaExceededError(), 429),
        (ProviderError("secret"), 502),
        (APIValidationError("invalid"), 400),
    ],
)
def test_stable_errors(error: Exception, status: int) -> None:
    response = exception_handler(error, {})
    assert response is not None
    assert response.status_code == status
    assert "secret" not in str(response.data)


def test_region_input_rejects_a_list() -> None:
    serializer = RegionInput(data=[])
    assert not serializer.is_valid()


def test_unknown_error_answers_json_with_reference_and_logs_traceback(
    customer: tuple[Client, User, Tenant],
    monkeypatch: pytest.MonkeyPatch,
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    _, user, _ = customer

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("private failure detail")

    monkeypatch.setattr(TenantsView, "get", fail)
    client = Client(raise_request_exception=False)
    client.force_login(user)
    response = client.get("/api/tenants/")
    assert response.status_code == 500
    assert response["Content-Type"] == "application/json"
    assert "no-store" in response["Cache-Control"]
    body = response.json()
    assert set(body) == {"code", "message", "request_id"}
    assert body["code"] == "server_error"
    assert body["message"] == "The server could not complete this request."
    assert re.fullmatch(r"[0-9a-f]{32}", body["request_id"])
    assert "private failure detail" not in response.content.decode()
    records = [record for record in pyracast_logs.records if record.name == "pyracast.api"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].exc_info is not None
    assert records[0].getMessage() == (
        f"api_server_error request_id={body['request_id']} method=GET path=/api/tenants/ "
        "view=TenantsView"
    )


def test_missing_object_logs_its_model_and_answers_404(
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    response = exception_handler(Region.DoesNotExist(), {})
    assert response is not None
    assert response.status_code == 404
    assert response.data == {"code": "not_found"}
    [record] = [record for record in pyracast_logs.records if record.name == "pyracast.api"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "api_object_not_found model=Region"


def test_entrypoints(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyracast import cli, wsgi

    seen: list[bool] = []
    monkeypatch.setattr(cli, "execute_from_command_line", lambda: seen.append(True))
    cli.main()
    assert seen == [True]
    assert callable(wsgi.application)


def test_fixture_demo(settings: LazySettings) -> None:
    with pytest.raises(CommandError, match="PYRACAST_DEBUG"):
        call_command("fixture_demo", "--username", "demo")
    settings.DEBUG = True
    with pytest.raises(CommandError, match="superuser"):
        call_command("fixture_demo", "--username", "demo")
    User.objects.create_user(username="demo", is_superuser=True)
    path = call_command("fixture_demo", "--username", "demo")
    assert "/forecast/?date=" in path
    assert ForecastResult.objects.get().payload["complete"] is True
    reference = SiteBinding.objects.get()
    assert reference.capacity_kw == reference.dc_capacity_kw == 1000
    assert reference.azimuth_degrees == reference.losses_percent == 0


def test_binding_api_records_megawatt_reference_metadata(
    customer: tuple[Client, User, Tenant],
) -> None:
    customer_client, _, tenant = customer
    region_id = create_region(customer_client, tenant)
    admin_client, _ = verified_admin()
    previous_id = setup_binding(admin_client, region_id)
    previous = SiteBinding.objects.get(pk=previous_id)
    response = admin_client.post(
        f"/api/platform/regions/{region_id}/bindings/",
        {
            "provider_account_id": previous.provider_account_id,
            "site_id": previous.site_id,
            "capacity_kw": 1000,
            "dc_capacity_kw": 1000,
            "capacity_semantics_verified": True,
            "installation_date": "2026-11-01",
        },
        content_type="application/json",
    )
    assert response.status_code == 201
    reference = SiteBinding.objects.get(pk=response.json()["id"])
    assert reference.capacity_kw == reference.dc_capacity_kw == 1000
    assert reference.azimuth_degrees == reference.losses_percent == 0
    previous.refresh_from_db()
    assert previous.capacity_kw == 1
    assert previous.dc_capacity_kw is None


def test_binding_api_accepts_no_tilt_because_every_reference_is_horizontal(
    customer: tuple[Client, User, Tenant],
) -> None:
    customer_client, _, tenant = customer
    region_id = create_region(customer_client, tenant)
    admin_client, _ = verified_admin()
    previous = SiteBinding.objects.get(pk=setup_binding(admin_client, region_id))
    response = admin_client.post(
        f"/api/platform/regions/{region_id}/bindings/",
        {
            "provider_account_id": previous.provider_account_id,
            "site_id": previous.site_id,
            "capacity_kw": 1000,
            "capacity_semantics_verified": True,
            "installation_date": "2026-11-01",
            "tilt_degrees": 0,
        },
        content_type="application/json",
    )
    assert response.status_code == 400
    assert SiteBinding.objects.filter(region_id=region_id).count() == 1


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_admin_login_requires_password_and_totp() -> None:
    password = "test-only-" + "x" * 40
    user = User.objects.create_superuser(
        username="mfa-login", email="operator@example.com", password=password
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/session/").json()["csrf_token"]
    response = client.post(
        "/api/auth/browser/v1/auth/login",
        {"email": user.email, "password": password},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 401
    assert not response.json()["meta"]["is_authenticated"]
    assert client.get("/admin/").status_code == 302
    assert client.get("/api/platform/accounts/").status_code == 403
    response = client.post(
        "/api/auth/browser/v1/auth/2fa/authenticate",
        {"code": format_hotp_value(hotp_value(secret, int(time()) // 30))},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 200, response.json()
    assert client.get("/admin/").status_code == 200
    assert client.get("/api/platform/accounts/").status_code == 405
