import logging
import re
from datetime import date
from time import time
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.totp.internal.auth import (
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
)
from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.test import Client, RequestFactory
from django.utils import timezone
from rest_framework.exceptions import ValidationError as APIValidationError

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.api.errors import exception_handler
from pyracast.api.serializers import RegionInput
from pyracast.api.views import TenantsView
from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import SiteBinding
from pyracast.connections.operations import QuotaExceededError, create_account, validate_binding
from pyracast.forecasts.jobs import collect
from pyracast.forecasts.models import ForecastResult
from pyracast.platform_admin.records import ReadOnlyAdmin
from pyracast.platform_admin.site import site
from pyracast.providers import FixtureProvider, adapter_for
from pyracast.regions.models import Region
from pyracast.regions.operations import (
    approve_region,
    configure_binding,
    submit_region,
    transition_region,
)
from tests.support.api import verified_admin

pytestmark = pytest.mark.django_db


@pytest.fixture
def customer() -> tuple[Client, User, Tenant]:
    user = User.objects.create_user(username="customer")
    tenant = Tenant.objects.create(name="Customer")
    Membership.objects.create(user=user, tenant=tenant, role=WorkspaceRole.ADMIN)
    client = Client()
    client.force_login(user)
    return client, user, tenant


def region_input() -> dict[str, Any]:
    return {"name": "Reference", "latitude": 39.9, "longitude": 32.8, "timezone": "Europe/Istanbul"}


def setup_binding(operator: User, region_id: int) -> SiteBinding:
    account = create_account(operator, provider="fixture", external_account_id="canonical")
    return configure_binding(
        operator,
        region_id,
        provider_account_id=account.pk,
        site_id="reference",
        capacity_kw=1,
        capacity_semantics_verified=True,
        installation_date=date(2020, 1, 1),
    )


# The region forecast opens its own read-only snapshot transaction.
@pytest.mark.django_db(transaction=True)
def test_tenant_isolation_and_read_only_ownership(customer: tuple[Client, User, Tenant]) -> None:
    client, user, tenant = customer
    region = submit_region(user, tenant.pk, **region_input())
    own = f"/api/tenants/{tenant.pk}/regions/{region.pk}/"
    forecast = f"forecast/?date={date.today()}"
    assert client.get(f"{own}location-change-requests/").json()["count"] == 0
    other = Tenant.objects.create(name="Other")
    other_user = User.objects.create_user(username="other")
    Membership.objects.create(user=other_user, tenant=other, role=WorkspaceRole.ADMIN)
    client.force_login(other_user)
    assert client.get(f"{own}location-change-requests/").status_code == 403
    assert client.get(own + forecast).status_code == 403
    assert client.delete(own).status_code == 403
    foreign = f"/api/tenants/{other.pk}/regions/{region.pk}/"
    assert client.get(f"{foreign}location-change-requests/").status_code == 404
    assert client.get(foreign + forecast).status_code == 404
    assert Client().get(own + forecast).status_code == 401
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(user)
    assert csrf.delete(own).status_code == 403
    assert (
        csrf.post(
            "/api/auth/browser/v1/auth/login",
            {"email": "customer@example.com", "password": "test"},
            content_type="application/json",
        ).status_code
        == 403
    )
    region.refresh_from_db()
    assert region.deleted_at is None


# The region forecast opens its own read-only snapshot transaction.
@pytest.mark.django_db(transaction=True)
def test_fixture_workflow_and_freshness(customer: tuple[Client, User, Tenant]) -> None:
    client, user, tenant = customer
    region = submit_region(user, tenant.pk, **region_input())
    admin_client, operator = verified_admin()
    binding = setup_binding(operator, region.pk)
    with pytest.raises(ValidationError):
        approve_region(operator, region.pk, binding.pk, timezone.now())
    validate_binding(operator, binding.pk, adapter_for(binding), timezone.now())
    job = approve_region(operator, region.pk, binding.pk, timezone.now())
    path = f"/api/tenants/{tenant.pk}/regions/{region.pk}/forecast/?date={job.target_date}"
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
    transition_region(operator, region.pk, "suspended")
    assert client.get(path).json()["freshness"] == "stale"
    region.timezone = "America/New_York"
    region.save(update_fields=["timezone"])
    assert client.get(path).json()["timezone"] == "Europe/Istanbul"
    assert client.get(path).json()["binding_id"] == binding.pk
    assert client.get(path.replace(str(job.target_date), "1900-01-01")).status_code == 400
    assert client.get(path.replace(str(job.target_date), "invalid")).status_code == 400
    assert admin_client.get("/admin/").status_code == 200
    assert admin_client.get("/admin/regions/region/").status_code == 200


def test_mfa_and_admin_read_only_boundaries(customer: tuple[Client, User, Tenant]) -> None:
    client, user, _ = customer
    user.is_staff = True
    user.is_superuser = True
    user.save()
    assert client.get("/admin/").status_code == 302
    _, admin_user = verified_admin()
    request = RequestFactory().post("/admin/")
    request.user = admin_user
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
        (ProviderError("secret"), 503),
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
    assert response.data == {"code": "not_found", "message": "The requested item was not found."}
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


def test_binding_records_megawatt_reference_metadata(
    customer: tuple[Client, User, Tenant],
) -> None:
    _, user, tenant = customer
    region = submit_region(user, tenant.pk, **region_input())
    _, operator = verified_admin()
    previous = setup_binding(operator, region.pk)
    reference = configure_binding(
        operator,
        region.pk,
        provider_account_id=previous.provider_account_id,
        site_id=previous.site_id,
        capacity_kw=1000,
        dc_capacity_kw=1000,
        capacity_semantics_verified=True,
        installation_date=date(2026, 11, 1),
    )
    assert reference.pk != previous.pk
    assert reference.capacity_kw == reference.dc_capacity_kw == 1000
    assert reference.azimuth_degrees == reference.losses_percent == 0
    previous.refresh_from_db()
    assert previous.capacity_kw == 1
    assert previous.dc_capacity_kw is None


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
    response = client.post(
        "/api/auth/browser/v1/auth/2fa/authenticate",
        {"code": format_hotp_value(hotp_value(secret, int(time()) // 30))},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 200, response.json()
    assert client.get("/admin/").status_code == 200
