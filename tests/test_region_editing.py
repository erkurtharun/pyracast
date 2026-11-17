from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, connection
from django.test import Client
from django.urls import reverse

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import SiteBinding
from pyracast.connections.operations import create_account, validate_binding
from pyracast.regions.models import AuditEvent, Region, RegionLocationChangeRequest
from pyracast.regions.operations import (
    approve_region,
    configure_binding,
    request_location_change,
    review_location_change,
    update_region,
)
from pyracast.trash import purge_expired, trash_region
from tests.test_api import verified_admin
from tests.test_domain import NOW, FakeAdapter, System, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_member_rename_preserves_approved_configuration_and_is_idempotent() -> None:
    system = make_system(approved=True)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
    response = client.patch(path, {"name": " New name "}, content_type="application/json")
    assert response.status_code == 200
    system.region.refresh_from_db()
    assert system.region.name == "New name"
    assert (system.region.revision, system.region.status, system.region.approved_binding_id) == (
        1,
        "approved",
        system.binding.pk,
    )
    update_region(system.member, system.region.pk, system.tenant.pk, name="New name")
    assert AuditEvent.objects.filter(action="region_renamed").count() == 1
    for field, value in (("latitude", 0), ("timezone", "UTC"), ("surface", "tilted")):
        response = client.patch(
            path, {"name": "Unsafe", field: value}, content_type="application/json"
        )
        assert response.status_code == 400
    assert client.put(path, {"name": "Changed"}, content_type="application/json").status_code == 200
    system.region.refresh_from_db()
    assert (system.region.latitude, system.region.timezone) == (40, "Europe/Istanbul")


def test_location_request_api_tenant_boundary_validation_and_soft_deletion() -> None:
    system = make_system(approved=True)
    client = Client()
    client.force_login(system.member)
    base = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
    path = f"{base}location-change-requests/"
    data = {"latitude": 41, "longitude": 30, "timezone": "UTC", "expected_revision": 1}
    assert (
        client.post(
            path, {**data, "status": "approved"}, content_type="application/json"
        ).status_code
        == 400
    )
    assert (
        client.post(
            path, {**data, "timezone": "invalid"}, content_type="application/json"
        ).status_code
        == 400
    )
    response = client.post(path, data, content_type="application/json")
    assert response.status_code == 201
    assert response.json()["status"] == "pending"
    assert "requested_by" not in response.json()
    assert (
        client.post(path, data, content_type="application/json").json()["id"]
        == response.json()["id"]
    )
    assert client.get(path).json()["count"] == 1
    assert (
        client.post(path, {**data, "latitude": 42}, content_type="application/json").status_code
        == 400
    )
    system.region.refresh_from_db()
    assert (system.region.latitude, system.region.status, system.region.approved_binding_id) == (
        40,
        "approved",
        system.binding.pk,
    )
    other = Tenant.objects.create(name="Other")
    other_member = User.objects.create(username="other")
    Membership.objects.create(user=other_member, tenant=other, role=Membership.Role.ADMIN)
    client.force_login(other_member)
    assert client.get(path).status_code == 403
    assert client.post(path, data, content_type="application/json").status_code == 403
    own_path = path.replace(f"tenants/{system.tenant.pk}", f"tenants/{other.pk}")
    assert client.get(own_path).status_code == 404
    assert client.post(own_path, data, content_type="application/json").status_code == 404
    assert (
        client.patch(base, {"name": "Forbidden"}, content_type="application/json").status_code
        == 403
    )
    client.force_login(system.member)
    trash_region(system.member, system.tenant.pk, system.region.pk)
    assert (
        client.patch(base, {"name": "Deleted"}, content_type="application/json").status_code == 404
    )
    assert client.get(path).status_code == 404
    assert client.post(path, data, content_type="application/json").status_code == 404
    with pytest.raises(Region.DoesNotExist):
        review_location_change(system.admin, response.json()["id"], approve=True)
    assert RegionLocationChangeRequest.objects.get().status == "pending"


def test_location_review_requires_new_binding_and_follows_binding_revisions() -> None:
    system = make_system(approved=True)
    change = request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=1,
        latitude=41,
        longitude=30,
        timezone="UTC",
        reason="Correct the reference site",
    )
    with pytest.raises(PermissionDenied):
        review_location_change(system.member, change.pk, approve=True)
    reviewed = review_location_change(system.admin, change.pk, approve=True)
    assert reviewed.reviewed_by_id == system.admin.pk
    assert reviewed.reviewed_at is not None
    assert review_location_change(system.admin, change.pk, approve=True).pk == change.pk
    with pytest.raises(ValidationError, match="already_reviewed"):
        review_location_change(system.admin, change.pk, approve=False)
    system.region.refresh_from_db()
    assert (system.region.latitude, system.region.longitude, system.region.timezone) == (
        41,
        30,
        "UTC",
    )
    assert (system.region.revision, system.region.status, system.region.approved_binding_id) == (
        2,
        "pending",
        None,
    )
    system.binding.refresh_from_db()
    assert (system.binding.latitude, system.binding.longitude, system.binding.region_revision) == (
        40,
        29,
        1,
    )
    with pytest.raises(SiteBinding.DoesNotExist):
        approve_region(system.admin, system.region.pk, system.binding.pk, NOW)
    assert AuditEvent.objects.filter(action="location_change_approved").count() == 1
    second = request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=2,
        latitude=42,
        longitude=31,
        timezone="UTC",
    )
    configure_binding(
        system.admin,
        system.region.pk,
        provider_account_id=system.account.pk,
        site_id="new",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    configure_binding(
        system.admin,
        system.region.pk,
        provider_account_id=system.account.pk,
        site_id="newer",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    second.refresh_from_db()
    assert second.base_revision == 3
    rejected = review_location_change(system.admin, second.pk, approve=False)
    assert rejected.status == "rejected"
    assert review_location_change(system.admin, second.pk, approve=False).pk == second.pk
    system.region.refresh_from_db()
    assert (system.region.latitude, system.region.revision) == (41, 3)


def test_approved_location_requires_validated_replacement_before_collection_resumes() -> None:
    system = make_system(approved=True)
    change = request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=1,
        latitude=41,
        longitude=30,
        timezone="UTC",
    )
    review_location_change(system.admin, change.pk, approve=True)
    replacement = configure_binding(
        system.admin,
        system.region.pk,
        provider_account_id=system.account.pk,
        site_id="new-location",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    assert (replacement.latitude, replacement.longitude, replacement.timezone) == (41, 30, "UTC")
    with pytest.raises(ValidationError, match="validated first"):
        approve_region(system.admin, system.region.pk, replacement.pk, NOW)
    validate_binding(system.admin, replacement.pk, FakeAdapter(), NOW)
    job = approve_region(system.admin, system.region.pk, replacement.pk, NOW)
    system.region.refresh_from_db()
    assert (system.region.status, system.region.approved_binding_id) == ("approved", replacement.pk)
    assert job.binding_id == replacement.pk


def test_request_operation_validation_permissions_and_rejection_keep_active_region() -> None:
    system = make_system(approved=True)
    with pytest.raises(ValidationError, match="1000"):
        request_location_change(
            system.member,
            system.region.pk,
            system.tenant.pk,
            expected_revision=1,
            reason="x" * 1001,
            latitude=41,
            longitude=30,
            timezone="UTC",
        )
    with pytest.raises(ValidationError, match="region_revision_changed"):
        request_location_change(
            system.member,
            system.region.pk,
            system.tenant.pk,
            expected_revision=2,
            latitude=41,
            longitude=30,
            timezone="UTC",
        )
    with pytest.raises(ValidationError, match="location_unchanged"):
        request_location_change(
            system.member,
            system.region.pk,
            system.tenant.pk,
            expected_revision=1,
            latitude=40,
            longitude=29,
            timezone="Europe/Istanbul",
        )
    other = Tenant.objects.create(name="Other")
    with pytest.raises(PermissionDenied):
        request_location_change(
            system.member,
            system.region.pk,
            other.pk,
            expected_revision=1,
            latitude=41,
            longitude=30,
            timezone="UTC",
        )
    Membership.objects.create(user=system.member, tenant=other, role=Membership.Role.ADMIN)
    with pytest.raises(Region.DoesNotExist):
        request_location_change(
            system.member,
            system.region.pk,
            other.pk,
            expected_revision=1,
            latitude=41,
            longitude=30,
            timezone="UTC",
        )
    change = request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=1,
        latitude=41,
        longitude=30,
        timezone="UTC",
    )
    review_location_change(system.admin, change.pk, approve=False)
    system.region.refresh_from_db()
    assert (
        system.region.latitude,
        system.region.revision,
        system.region.status,
        system.region.approved_binding_id,
    ) == (40, 1, "approved", system.binding.pk)


def test_operator_admin_review_actions_and_stale_request_errors() -> None:
    system = make_system(approved=True)
    client, _ = verified_admin()
    change = request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=1,
        latitude=41,
        longitude=30,
        timezone="UTC",
    )
    path = "/admin/regions/regionlocationchangerequest/"
    assert client.get(path).status_code == 200
    assert client.get(f"{path}{change.pk}/change/").status_code == 200
    assert (
        client.post(
            path, {"action": "reject", "_selected_action": [change.pk]}, follow=True
        ).status_code
        == 200
    )
    change.refresh_from_db()
    assert change.status == "rejected"
    error = client.post(path, {"action": "approve", "_selected_action": [change.pk]}, follow=True)
    assert b"stale or already reviewed" in error.content
    new = request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=1,
        latitude=42,
        longitude=30,
        timezone="UTC",
    )
    assert (
        client.post(
            path, {"action": "approve", "_selected_action": [new.pk]}, follow=True
        ).status_code
        == 200
    )
    new.refresh_from_db()
    assert new.status == "approved"


@pytest.mark.parametrize("same_values", [True, False])
def test_concurrent_requests_preserve_one_pending_request(same_values: bool) -> None:
    system = make_system(approved=True)
    barrier = Barrier(2)

    def submit(latitude: float) -> int | str:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return request_location_change(
                system.member,
                system.region.pk,
                system.tenant.pk,
                expected_revision=1,
                latitude=latitude,
                longitude=30,
                timezone="UTC",
            ).pk
        except ValidationError:
            return "pending_conflict"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, latitude) for latitude in (41, 41 if same_values else 42)]
        results = [future.result(timeout=15) for future in futures]
    assert RegionLocationChangeRequest.objects.count() == 1
    assert len(set(results)) == (1 if same_values else 2)
    assert results.count("pending_conflict") == (0 if same_values else 1)


def test_concurrent_reviews_apply_location_at_most_once() -> None:
    system = make_system(approved=True)
    change = request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=1,
        latitude=41,
        longitude=30,
        timezone="UTC",
    )
    barrier = Barrier(2)

    def approve() -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return review_location_change(system.admin, change.pk, approve=True).status
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve) for _ in range(2)]
        assert [future.result(timeout=15) for future in futures] == ["approved", "approved"]
    system.region.refresh_from_db()
    assert system.region.revision == 2
    assert AuditEvent.objects.filter(action="location_change_approved").count() == 1


def test_purge_removes_location_requests_without_deleting_review_actors() -> None:
    from datetime import timedelta

    from django.utils import timezone

    system = make_system()
    change = request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=1,
        latitude=41,
        longitude=30,
        timezone="UTC",
    )
    review_location_change(system.admin, change.pk, approve=False)
    trash_region(system.member, system.tenant.pk, system.region.pk)
    assert purge_expired(timezone.now() + timedelta(days=31))["regions"] == 1
    assert not RegionLocationChangeRequest.objects.exists()
    assert User.objects.filter(pk__in=(system.member.pk, system.admin.pk)).count() == 2


BINDING_SUBMISSION: dict[str, Any] = {"site_id": "site", "capacity_kw": 1}


def submit_binding(system: System, **changes: Any) -> SiteBinding:
    return configure_binding(
        system.admin,
        system.region.pk,
        **{
            "provider_account_id": system.account.pk,
            "capacity_semantics_verified": True,
            **BINDING_SUBMISSION,
            **changes,
        },
    )


def test_identical_binding_save_returns_the_existing_binding() -> None:
    system = make_system(approved=True)
    events = AuditEvent.objects.count()
    assert submit_binding(system, site_id=" site ", capacity_kw=1.0).pk == system.binding.pk
    system.region.refresh_from_db()
    assert (system.region.revision, system.region.status, system.region.approved_binding_id) == (
        1,
        "approved",
        system.binding.pk,
    )
    assert SiteBinding.objects.count() == 1
    assert AuditEvent.objects.count() == events


def _stored(field: str, value: object) -> Callable[[System], dict[str, Any]]:
    def change(system: System) -> dict[str, Any]:
        SiteBinding.objects.filter(pk=system.binding.pk).update(**{field: value})
        return {}

    return change


def _other_account(system: System) -> dict[str, Any]:
    account = create_account(
        system.admin, provider="fixture", external_account_id="other", api_key=""
    )
    return {"provider_account_id": account.pk}


BINDING_FIELDS_OUTSIDE_SETUP = {
    "id",
    "region",
    "region_revision",
    "validated_at",
    "validated_credential_revision",
    "health",
    "created_at",
}
BINDING_FIELD_CHANGES: dict[str, Callable[[System], dict[str, Any]]] = {
    "provider_account": _other_account,
    "site_id": lambda system: {"site_id": "other"},
    "capacity_kw": lambda system: {"capacity_kw": 2},
    "dc_capacity_kw": lambda system: {"dc_capacity_kw": 1.2},
    "power_basis": lambda system: {"power_basis": "dc"},
    "capacity_semantics_verified": _stored("capacity_semantics_verified", False),
    "azimuth_degrees": lambda system: {"azimuth_degrees": 10},
    "losses_percent": lambda system: {"losses_percent": 5},
    "installation_date": lambda system: {"installation_date": date(2020, 1, 1)},
    "settings_verification_reference": lambda system: {
        "settings_verification_reference": "Checked with the account owner"
    },
    "latitude": _stored("latitude", 40.5),
    "longitude": _stored("longitude", 29.5),
    "timezone": _stored("timezone", "UTC"),
}


@pytest.mark.parametrize(
    "field",
    [
        field.name
        for field in SiteBinding._meta.concrete_fields
        if field.name not in BINDING_FIELDS_OUTSIDE_SETUP
    ],
)
def test_a_change_of_any_binding_setup_field_replaces_the_binding(field: str) -> None:
    system = make_system(approved=True)
    if field == "tilt_degrees":
        with pytest.raises(ValidationError):
            submit_binding(system, tilt_degrees=5)
        with pytest.raises(IntegrityError):
            SiteBinding.objects.filter(pk=system.binding.pk).update(tilt_degrees=5)
        return
    replacement = submit_binding(system, **BINDING_FIELD_CHANGES[field](system))
    assert replacement.pk != system.binding.pk
    system.region.refresh_from_db()
    assert (system.region.revision, system.region.status, system.region.approved_binding_id) == (
        2,
        "pending",
        None,
    )
    assert replacement.region_revision == 2


def _pending_location_request(system: System) -> RegionLocationChangeRequest:
    return request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=system.region.revision,
        latitude=41,
        longitude=30,
        timezone="UTC",
    )


def test_binding_replacement_rebases_pending_location_request() -> None:
    system = make_system(approved=True)
    change = _pending_location_request(system)
    submit_binding(system)
    change.refresh_from_db()
    assert change.base_revision == 1
    submit_binding(system, site_id="replacement")
    change.refresh_from_db()
    system.region.refresh_from_db()
    assert change.base_revision == system.region.revision == 2
    rebased = AuditEvent.objects.get(action="location_change_rebased")
    assert (rebased.actor_id, rebased.region_id, rebased.revision) == (
        system.admin.pk,
        system.region.pk,
        2,
    )
    assert _pending_location_request(system).pk == change.pk
    assert review_location_change(system.admin, change.pk, approve=True).status == "approved"
    system.region.refresh_from_db()
    assert (system.region.latitude, system.region.revision) == (41, 3)


def test_concurrent_location_request_and_binding_replacement_leave_no_stale_request() -> None:
    system = make_system(approved=True)
    barrier = Barrier(2)

    def request() -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            _pending_location_request(system)
            return "requested"
        except ValidationError as error:
            return str(error.messages[0])
        finally:
            connection.close()

    def replace() -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            submit_binding(system, site_id="replacement")
            return "replaced"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request), pool.submit(replace)]
        results = [future.result(timeout=15) for future in futures]
    assert results[1] == "replaced"
    assert results[0] in {"requested", "region_revision_changed"}
    system.region.refresh_from_db()
    assert system.region.revision == 2
    assert not RegionLocationChangeRequest.objects.filter(status="pending").exclude(
        base_revision=system.region.revision
    )


def test_request_left_on_an_earlier_revision_cannot_be_approved() -> None:
    system = make_system(approved=True)
    change = _pending_location_request(system)
    Region.objects.filter(pk=system.region.pk).update(revision=2)
    with pytest.raises(ValidationError, match="region_revision_changed"):
        review_location_change(system.admin, change.pk, approve=True)
    client, _ = verified_admin()
    response = client.get(reverse("admin:regions_region_setup", args=(system.region.pk,)))
    assert response.status_code == 200
    assert b"made for an earlier revision" in response.content
    assert b'value="approve_location"' not in response.content
    assert b'value="reject_location"' in response.content
