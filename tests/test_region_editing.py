from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.test import Client

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import SiteBinding
from pyracast.connections.operations import validate_binding
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
from tests.test_domain import NOW, FakeAdapter, make_system

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


def test_location_review_rejects_stale_revision_and_requires_new_binding() -> None:
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
    with pytest.raises(ValidationError, match="region_revision_changed"):
        review_location_change(system.admin, second.pk, approve=True)
    with pytest.raises(ValidationError, match="location_change_already_pending"):
        request_location_change(
            system.member,
            system.region.pk,
            system.tenant.pk,
            expected_revision=3,
            latitude=42,
            longitude=31,
            timezone="UTC",
        )
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
