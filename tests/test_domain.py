from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction

from pyracast.accounts.models import Membership, Tenant
from pyracast.accounts.operations import require_admin, require_member
from pyracast.connections.contracts import ProviderData, ProviderFailure
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.operations import (
    QuotaExhausted,
    create_account,
    reserve_request,
    rotate_secret,
    validate_binding,
)
from pyracast.forecasts.models import CollectionJob, RequestAttempt
from pyracast.regions.models import AuditEvent, Region
from pyracast.regions.operations import (
    approve_region,
    configure_binding,
    submit_region,
    transition_region,
    update_region,
    validate_location,
)

NOW = datetime(2026, 11, 9, 13, tzinfo=UTC)
pytestmark = pytest.mark.django_db(transaction=True)


def secret_ref(name: str = "TEST_KEY") -> str:
    return f"env:{name}"


class FakeAdapter:
    def __init__(self, action: Callable[[], None] = lambda: None) -> None:
        self.action = action

    def validate(self, binding: SiteBinding) -> None:
        assert not connection.in_atomic_block
        self.action()

    def fetch(self, binding: SiteBinding, target_date: object) -> ProviderData:
        assert not connection.in_atomic_block
        self.action()
        return ProviderData({"fixture": True}, [])


@dataclass
class System:
    member: User
    admin: User
    tenant: Tenant
    region: Region
    account: ProviderAccount
    binding: SiteBinding


def make_system(*, approved: bool = False) -> System:
    member = User.objects.create(username="member")
    admin = User.objects.create(username="admin", is_superuser=True)
    tenant = Tenant.objects.create(name="Tenant")
    Membership.objects.create(user=member, tenant=tenant)
    region = submit_region(
        member, tenant.pk, name="Site", latitude=40, longitude=29, timezone="Europe/Istanbul"
    )
    account = create_account(
        admin, provider="fixture", external_account_id="customer", secret_reference=secret_ref()
    )
    binding = configure_binding(
        admin,
        region.pk,
        provider_account_id=account.pk,
        site_id="site",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    if approved:
        validate_binding(admin, binding.pk, FakeAdapter(), NOW - timedelta(hours=1))
        approve_region(admin, region.pk, binding.pk, NOW)
    return System(member, admin, tenant, region, account, binding)


def test_tenant_and_platform_authorization() -> None:
    system = make_system()
    other = Tenant.objects.create(name="Other")
    with pytest.raises(PermissionDenied):
        require_member(system.member, other.pk)
    with pytest.raises(PermissionDenied):
        require_admin(system.member)
    system.admin.is_active = False
    with pytest.raises(PermissionDenied):
        require_admin(system.admin)
    system.member.is_active = False
    with pytest.raises(PermissionDenied):
        require_member(system.member, system.tenant.pk)
    system.member.is_active = True
    Membership.objects.create(user=system.member, tenant=other)
    with pytest.raises(Region.DoesNotExist):
        update_region(
            system.member,
            system.region.pk,
            other.pk,
            name="Other",
            latitude=0,
            longitude=0,
            timezone="UTC",
        )


@pytest.mark.parametrize(
    ("latitude", "longitude", "timezone"),
    [
        (float("nan"), 0, "UTC"),
        (91, 0, "UTC"),
        (0, float("inf"), "UTC"),
        (0, -181, "UTC"),
        (0, 0, "Invalid/Zone"),
        (0, 0, "/absolute"),
    ],
)
def test_location_rejects_invalid_values(latitude: float, longitude: float, timezone: str) -> None:
    with pytest.raises(ValidationError):
        validate_location(latitude, longitude, timezone)


@pytest.mark.parametrize("name", [" ", "x" * 201])
def test_region_names_are_bounded(name: str) -> None:
    system = make_system()
    with pytest.raises(ValidationError):
        submit_region(
            system.member, system.tenant.pk, name=name, latitude=0, longitude=0, timezone="UTC"
        )
    with pytest.raises(ValidationError):
        update_region(
            system.member,
            system.region.pk,
            system.tenant.pk,
            name=name,
            latitude=0,
            longitude=0,
            timezone="UTC",
        )


def test_revision_invalidates_approval_and_preserves_previous_configuration() -> None:
    system = make_system(approved=True)
    changed = update_region(
        system.member,
        system.region.pk,
        system.tenant.pk,
        name="New",
        latitude=10,
        longitude=20,
        timezone="UTC",
    )
    assert (changed.revision, changed.status, changed.approved_binding_id) == (2, "pending", None)
    system.binding.refresh_from_db()
    assert (system.binding.latitude, system.binding.region_revision) == (40, 1)
    with pytest.raises(SiteBinding.DoesNotExist):
        approve_region(system.admin, changed.pk, system.binding.pk, NOW)


@pytest.mark.parametrize(
    "options",
    [
        {"site_id": ""},
        {"site_id": "x" * 201},
        {"capacity_kw": 0},
        {"capacity_kw": float("nan")},
        {"power_basis": "xx"},
        {"capacity_basis": "dc"},
        {"tilt_degrees": 1},
        {"capacity_semantics_verified": False},
        {"azimuth_degrees": 360},
        {"azimuth_degrees": float("inf")},
        {"losses_percent": 100},
        {"losses_percent": float("nan")},
    ],
)
def test_invalid_bindings_are_rejected(options: dict[str, Any]) -> None:
    system = make_system()
    arguments: dict[str, Any] = {
        "provider_account_id": system.account.pk,
        "site_id": "site",
        "capacity_kw": 1,
        "capacity_semantics_verified": True,
    }
    arguments.update(options)
    with pytest.raises(ValidationError):
        configure_binding(system.admin, system.region.pk, **arguments)


@pytest.mark.parametrize(
    "options",
    [
        {"provider": "unknown"},
        {"external_account_id": " "},
        {"secret_reference": "literal"},
        {"secret_reference": "env:bad-name"},
        {"daily_quota": 0},
        {"daily_quota": 11},
    ],
)
def test_account_input_validation(options: dict[str, Any]) -> None:
    system = make_system()
    arguments: dict[str, Any] = {
        "provider": "fixture",
        "external_account_id": "another",
        "secret_reference": "env:TEST_KEY",
    }
    arguments.update(options)
    with pytest.raises(ValidationError):
        create_account(system.admin, **arguments)


def test_canonical_accounts_share_quota_and_rotation_invalidates_validation() -> None:
    system = make_system(approved=True)
    duplicate = create_account(
        system.admin,
        provider=" FIXTURE ",
        external_account_id=" CUSTOMER ",
        secret_reference=secret_ref("OTHER_KEY"),
    )
    assert duplicate.pk == system.account.pk
    assert duplicate.secret_reference == secret_ref()
    rotate_secret(system.admin, system.account.pk, "env:NEW_KEY")
    system.account.refresh_from_db()
    assert system.account.credential_revision == 2
    with pytest.raises(ValidationError):
        approve_region(system.admin, system.region.pk, system.binding.pk, NOW)
    with pytest.raises(ValidationError):
        rotate_secret(system.admin, system.account.pk, "raw-secret")
    with pytest.raises(ValidationError):
        rotate_secret(system.admin, system.account.pk, "env:bad-name")


def test_validation_failure_consumes_quota_without_secret_in_audit() -> None:
    system = make_system()

    def fail() -> None:
        raise ProviderFailure("do not persist this secret")

    with pytest.raises(ProviderFailure):
        validate_binding(system.admin, system.binding.pk, FakeAdapter(fail), NOW)
    system.binding.refresh_from_db()
    assert (system.binding.health, system.binding.validated_at) == ("error", None)
    assert RequestAttempt.objects.get().outcome == "failed"
    assert not AuditEvent.objects.filter(action="connection_validated").exists()


@pytest.mark.parametrize("change", ["revision", "credential"])
def test_validation_rechecks_configuration_after_network(change: str) -> None:
    system = make_system()

    def mutate() -> None:
        if change == "revision":
            Region.objects.filter(pk=system.region.pk).update(revision=2)
        else:
            rotate_secret(system.admin, system.account.pk, "env:NEW_KEY")

    with pytest.raises(ValidationError):
        validate_binding(system.admin, system.binding.pk, FakeAdapter(mutate), NOW)
    assert RequestAttempt.objects.get().outcome == "uncertain"


def test_approval_is_atomic_idempotent_and_permissions_checked() -> None:
    system = make_system()
    with pytest.raises(ValidationError):
        approve_region(system.admin, system.region.pk, system.binding.pk, NOW)
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    first = approve_region(system.admin, system.region.pk, system.binding.pk, NOW)
    duplicate = approve_region(
        system.admin, system.region.pk, system.binding.pk, NOW + timedelta(seconds=1)
    )
    assert first.pk == duplicate.pk
    assert first.target_date.isoformat() == "2026-11-10"
    assert AuditEvent.objects.filter(action="approved").count() == 1
    assert CollectionJob.objects.count() == 1
    for status in ("suspended", "rejected"):
        assert transition_region(system.admin, system.region.pk, status).status == status
    with pytest.raises(ValidationError):
        transition_region(system.admin, system.region.pk, "approved")
    with pytest.raises(ValidationError):
        approve_region(system.admin, system.region.pk, system.binding.pk, NOW.replace(tzinfo=None))


def test_utc_quota_is_conservative_and_resets_on_utc_midnight() -> None:
    system = make_system()
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=1)
    first = reserve_request(system.binding, NOW, purpose="validation")
    assert first.utc_day == NOW.date()
    assert first.outcome == "uncertain"
    with pytest.raises(QuotaExhausted):
        reserve_request(system.binding, NOW, purpose="forecast")
    assert (
        reserve_request(system.binding, NOW + timedelta(days=1), purpose="forecast").pk != first.pk
    )
    ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=False)
    with pytest.raises(QuotaExhausted):
        reserve_request(system.binding, NOW + timedelta(days=2), purpose="forecast")
    with pytest.raises(ValidationError):
        reserve_request(system.binding, NOW.replace(tzinfo=None), purpose="forecast")


def test_database_rejects_duplicate_canonical_account_and_membership() -> None:
    system = make_system()
    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.create(user=system.member, tenant=system.tenant)
    with pytest.raises(IntegrityError), transaction.atomic():
        ProviderAccount.objects.create(
            provider=" FIXTURE ",
            external_account_id="CUSTOMER",
            secret_reference=secret_ref("OTHER"),
        )


def test_reconfiguring_a_binding_creates_new_revision_requiring_approval() -> None:
    system = make_system(approved=True)
    replacement = configure_binding(
        system.admin,
        system.region.pk,
        provider_account_id=system.account.pk,
        site_id="replacement",
        capacity_kw=2,
        capacity_semantics_verified=True,
    )
    system.region.refresh_from_db()
    assert replacement.region_revision == system.region.revision == 2
    assert system.region.status == "pending"
    assert system.region.approved_binding_id is None
    system.binding.refresh_from_db()
    assert (system.binding.site_id, system.binding.capacity_kw, system.binding.region_revision) == (
        "site",
        1,
        1,
    )


def test_live_account_requires_recorded_service_authorization() -> None:
    system = make_system()
    with pytest.raises(ValidationError):
        create_account(
            system.admin,
            provider="solcast",
            external_account_id="live-account",
            secret_reference=secret_ref(),
        )
    with pytest.raises(ValidationError):
        create_account(
            system.admin,
            provider="solcast",
            external_account_id="live-account",
            secret_reference=secret_ref(),
            authorization_reference="x" * 501,
        )
    live = create_account(
        system.admin,
        provider="solcast",
        external_account_id="live-account",
        secret_reference=secret_ref(),
        authorization_reference="service-grant-42",
    )
    assert live.authorization_reference == "service-grant-42"
    assert live.authorized_by_id == system.admin.pk
    assert live.authorized_at is not None
    ProviderAccount.objects.filter(pk=system.account.pk).update(authorized_at=None)
    with pytest.raises(QuotaExhausted):
        reserve_request(system.binding, NOW, purpose="validation")


def test_live_binding_requires_explicit_operator_settings_attestation() -> None:
    system = make_system()
    live = create_account(
        system.admin,
        provider="solcast",
        external_account_id="live",
        secret_reference=secret_ref(),
        authorization_reference="service-grant",
    )
    options: dict[str, Any] = {
        "provider_account_id": live.pk,
        "site_id": "live-site",
        "capacity_kw": 1,
        "capacity_semantics_verified": True,
    }
    for additions in (
        {},
        {"settings_verification_reference": "checked"},
        {"settings_verification_reference": "x" * 501},
    ):
        with pytest.raises(ValidationError):
            configure_binding(system.admin, system.region.pk, **(options | additions))
    binding = configure_binding(
        system.admin,
        system.region.pk,
        **options,
        installation_date=NOW.date(),
        settings_verification_reference="operator-check-42",
    )
    assert binding.settings_verification_reference == "operator-check-42"
    SiteBinding.objects.filter(pk=binding.pk).update(settings_verification_reference="")
    with pytest.raises(ValidationError):
        validate_binding(system.admin, binding.pk, FakeAdapter(), NOW)
    assert not RequestAttempt.objects.exists()
    SiteBinding.objects.filter(pk=binding.pk).update(settings_verification_reference="restored")
    validate_binding(system.admin, binding.pk, FakeAdapter(), NOW)
    approve_region(system.admin, system.region.pk, binding.pk, NOW)
    SiteBinding.objects.filter(pk=binding.pk).update(settings_verification_reference="")
    with pytest.raises(ValidationError):
        approve_region(system.admin, system.region.pk, binding.pk, NOW)
