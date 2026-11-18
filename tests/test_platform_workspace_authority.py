from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from pyracast.accounts.invitations import (
    invite_workspace_member,
    reissue_workspace_invitation,
    revoke_workspace_invitation,
)
from pyracast.accounts.models import (
    Membership,
    WorkspaceInvitation,
    WorkspaceMemberRemoval,
    WorkspaceRole,
)
from pyracast.accounts.operations import remove_workspace_member
from pyracast.plants.models import Plant, PlantSubmission
from pyracast.plants.operations import create_plant, revise_plant
from pyracast.plants.sites import submit_site
from pyracast.regions.models import Region, RegionLocationChangeRequest
from pyracast.regions.operations import request_location_change, submit_region
from tests.support.api import verified_admin
from tests.support.domain import System, make_system

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def platform_admin(username: str = "platform") -> User:
    return User.objects.create_superuser(username=username, email=f"{username}@example.com")


def viewer_of(system: System, username: str = "viewer") -> User:
    viewer = User.objects.create_user(username=username, email=f"{username}@example.com")
    Membership.objects.create(tenant=system.tenant, user=viewer)
    return viewer


def test_platform_authority_runs_workspace_operations_without_membership() -> None:
    system = make_system()
    platform = platform_admin()
    viewer = viewer_of(system)
    tenant_id = system.tenant.pk

    invitation = invite_workspace_member(platform, tenant_id, "guest@example.com", platform=True)
    assert (invitation.created_by, invitation.role) == (platform, WorkspaceRole.VIEWER)
    assert mail.outbox[-1].to == ["guest@example.com"]
    revoke_workspace_invitation(platform, tenant_id, invitation.pk, platform=True)
    invitation.refresh_from_db()
    assert invitation.expires_at <= timezone.now()

    remove_workspace_member(platform, tenant_id, viewer.pk, platform=True)
    assert not Membership.objects.filter(user=viewer).exists()
    assert WorkspaceMemberRemoval.objects.get().actor == platform

    site = submit_site(
        platform,
        tenant_id,
        platform=True,
        name="Console site",
        latitude=41,
        longitude=29,
        timezone="Europe/Istanbul",
        dc_capacity_mw=5,
    )
    assert site.tenant_id == tenant_id
    assert PlantSubmission.objects.get(plant__region=site).submitted_by == platform

    region = submit_region(
        platform,
        tenant_id,
        platform=True,
        name="Bare location",
        latitude=42,
        longitude=29,
        timezone="UTC",
    )
    plant = create_plant(
        platform, tenant_id, region.pk, platform=True, name="Bare plant", dc_capacity_mw=2
    )
    revise_plant(
        platform,
        tenant_id,
        region.pk,
        plant.pk,
        platform=True,
        name="Renamed plant",
        dc_capacity_mw=3,
    )
    assert PlantSubmission.objects.get(plant=plant).dc_capacity_mw == 3
    assert Plant.objects.get(pk=plant.pk).name == "Renamed plant"

    change = request_location_change(
        platform,
        region.pk,
        tenant_id,
        platform=True,
        expected_revision=1,
        latitude=42.5,
        longitude=29,
        timezone="UTC",
    )
    assert change.requested_by == platform
    assert not Membership.objects.filter(user=platform).exists()


def refused_actor(system: System, identity: str) -> User:
    if identity == "staff":
        return User.objects.create_user(username="staff", is_staff=True)
    if identity == "inactive":
        inactive = platform_admin("inactive")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        return inactive
    if identity == "viewer":
        return viewer_of(system, "plain-member")
    return system.member


@pytest.mark.parametrize("identity", ["staff", "inactive", "viewer", "workspace_admin"])
def test_platform_authority_refuses_everyone_who_is_not_a_platform_administrator(
    identity: str,
) -> None:
    system = make_system()
    tenant_id, region_id = system.tenant.pk, system.region.pk
    viewer = viewer_of(system)
    invitation = invite_workspace_member(system.member, tenant_id, "guest@example.com")
    plant = create_plant(system.member, tenant_id, region_id, name="Plant", dc_capacity_mw=1)
    actor = refused_actor(system, identity)
    operations: list[Callable[[], Any]] = [
        lambda: invite_workspace_member(actor, tenant_id, "other@example.com", platform=True),
        lambda: revoke_workspace_invitation(actor, tenant_id, invitation.pk, platform=True),
        lambda: reissue_workspace_invitation(actor, tenant_id, invitation.pk),
        lambda: remove_workspace_member(actor, tenant_id, viewer.pk, platform=True),
        lambda: submit_site(
            actor,
            tenant_id,
            platform=True,
            name="Refused site",
            latitude=41,
            longitude=29,
            timezone="UTC",
            dc_capacity_mw=1,
        ),
        lambda: submit_region(
            actor,
            tenant_id,
            platform=True,
            name="Refused",
            latitude=41,
            longitude=29,
            timezone="UTC",
        ),
        lambda: revise_plant(
            actor, tenant_id, region_id, plant.pk, platform=True, name="Refused", dc_capacity_mw=9
        ),
        lambda: request_location_change(
            actor,
            region_id,
            tenant_id,
            platform=True,
            expected_revision=system.region.revision,
            latitude=41,
            longitude=29,
            timezone="UTC",
        ),
    ]
    for operation in operations:
        with pytest.raises(PermissionDenied):
            operation()
    invitation.refresh_from_db()
    assert invitation.expires_at > timezone.now()
    assert WorkspaceInvitation.objects.count() == 1
    assert Membership.objects.filter(user=viewer).exists()
    assert Region.objects.filter(tenant_id=tenant_id).count() == 1
    assert PlantSubmission.objects.get(plant=plant).dc_capacity_mw == 1
    assert not RegionLocationChangeRequest.objects.exists()


def test_tenant_api_treats_a_platform_administrator_without_membership_as_an_outsider() -> None:
    system = make_system()
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="Plant", dc_capacity_mw=1
    )
    viewer = viewer_of(system)
    invitation = invite_workspace_member(system.member, system.tenant.pk, "guest@example.com")
    client, platform = verified_admin()
    base = f"/api/tenants/{system.tenant.pk}/"
    region = f"{base}regions/{system.region.pk}/"
    site = {
        "name": "API site",
        "latitude": 41,
        "longitude": 29,
        "timezone": "UTC",
        "dc_capacity_mw": 1,
    }
    requests = [
        ("get", f"{base}members/", None),
        ("delete", f"{base}members/{viewer.pk}/", None),
        ("get", f"{base}invitations/", None),
        ("post", f"{base}invitations/", {"email": "other@example.com"}),
        ("delete", f"{base}invitations/{invitation.pk}/", None),
        ("post", f"{base}sites/", site),
        ("post", f"{region}plants/", {"name": "Second", "dc_capacity_mw": 1}),
        ("put", f"{region}plants/{plant.pk}/", {"name": "Changed", "dc_capacity_mw": 2}),
        (
            "post",
            f"{region}location-change-requests/",
            {"expected_revision": 1, "latitude": 41, "longitude": 29, "timezone": "UTC"},
        ),
    ]
    for method, path, body in requests:
        response = getattr(client, method)(path, body, content_type="application/json")
        assert response.status_code == 403, path
        assert response.json()["code"] == (
            "membership_required" if method == "get" else "workspace_admin_required"
        ), path
    assert not Membership.objects.filter(user=platform).exists()
    assert Membership.objects.filter(user=viewer).exists()
    assert WorkspaceInvitation.objects.count() == 1
    assert Region.objects.count() == 1
    assert PlantSubmission.objects.get(plant=plant).dc_capacity_mw == 1
    assert not RegionLocationChangeRequest.objects.exists()
