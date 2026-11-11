from datetime import date

import pytest
from django.conf import LazySettings
from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, RequestFactory
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework.exceptions import ValidationError as APIValidationError

from pyracast.accounts.models import Membership, Tenant
from pyracast.admin import ReadOnlyAdmin, RegionAdmin, site
from pyracast.api.errors import exception_handler
from pyracast.api.serializers import RegionInput
from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderFailure
from pyracast.connections.operations import QuotaExhausted
from pyracast.forecasts.jobs import collect
from pyracast.forecasts.models import CollectionJob, ForecastResult
from pyracast.providers import FixtureProvider
from pyracast.regions.models import Region

pytestmark = pytest.mark.django_db


@pytest.fixture
def customer() -> tuple[Client, User, Tenant]:
    user = User.objects.create_user(username="customer")
    tenant = Tenant.objects.create(name="Customer")
    Membership.objects.create(user=user, tenant=tenant)
    client = Client()
    client.force_login(user)
    return client, user, tenant


def verified_admin() -> tuple[Client, User]:
    user = User.objects.create_user(username="operator", is_superuser=True, is_staff=True)
    device = TOTPDevice.objects.create(user=user, name="operator", confirmed=True)
    client = Client()
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
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
            "secret_reference": "env:FIXTURE",
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


def test_tenant_isolation_and_read_only_ownership(customer: tuple[Client, User, Tenant]) -> None:
    client, user, tenant = customer
    region_id = create_region(client, tenant)
    base = f"/api/tenants/{tenant.pk}/regions/"
    assert client.get(base).json()["count"] == 1
    assert client.get(f"{base}{region_id}/").json()["status"] == "pending"
    other = Tenant.objects.create(name="Other")
    other_user = User.objects.create_user(username="other")
    Membership.objects.create(user=other_user, tenant=other)
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
        csrf.post("/auth/login/", {"username": "customer", "password": "test"}).status_code == 403
    )


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
        (QuotaExhausted(), 429),
        (ProviderFailure("secret"), 502),
        (APIValidationError("invalid"), 400),
    ],
)
def test_stable_errors(error: Exception, status: int) -> None:
    response = exception_handler(error, {})
    assert response is not None
    assert response.status_code == status
    assert "secret" not in str(response.data)


def test_unknown_error_is_not_hidden() -> None:
    assert exception_handler(RuntimeError(), {}) is None
    serializer = RegionInput(data=[])
    assert not serializer.is_valid()


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


def test_admin_login_requires_password_and_totp() -> None:
    password = "test-only-" + "x" * 40
    user = User.objects.create_superuser(username="mfa-login", password=password)
    device = TOTPDevice.objects.create(user=user, name="initial", confirmed=True)
    client = Client(enforce_csrf_checks=True)
    assert client.get("/admin/login/").status_code == 200
    token = client.cookies["csrftoken"].value
    credentials = {"username": user.username, "password": password, "csrfmiddlewaretoken": token}
    assert client.post("/admin/login/", credentials).status_code == 200
    assert client.get("/admin/").status_code == 302
    credentials["otp_token"] = str(totp(device.bin_key)).zfill(6)
    credentials["otp_device"] = device.persistent_id
    response = client.post("/admin/login/", credentials)
    assert response.status_code == 302, response.context["form"].errors
    assert client.get("/admin/").status_code == 200
