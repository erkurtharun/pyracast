from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from pyracast.accounts.models import (
    InvitationRevocation,
    Membership,
    Tenant,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.plants.models import Plant
from pyracast.regions.operations import submit_region
from tests.support.api import verified_admin

pytestmark = pytest.mark.django_db


def member(tenant: Tenant, address: str, role: str, *, active: bool = True) -> User:
    user = User.objects.create_user(username=address, email=address, is_active=active)
    Membership.objects.create(tenant=tenant, user=user, role=role)
    return user


def plant(tenant: Tenant, actor: User, name: str, *, automated: bool) -> Plant:
    region = submit_region(
        actor, tenant.pk, name=name, latitude=40 + len(name) / 100, longitude=29, timezone="UTC"
    )
    created = Plant.objects.create(region=region, name=name)
    if automated:
        Plant.objects.filter(pk=created.pk).update(
            automatic_calibration_enabled=True,
            automatic_calibration_enabled_by=actor,
            next_calibration_check_at=timezone.now(),
        )
    return created


def invitation(
    tenant: Tenant, actor: User, address: str, *, open_: bool, role: str = WorkspaceRole.VIEWER
) -> None:
    WorkspaceInvitation.objects.create(
        tenant=tenant,
        email=address,
        role=role,
        created_by=actor,
        expires_at=timezone.now() + timedelta(days=1 if open_ else -1),
    )


def test_the_administrator_page_lists_what_a_change_would_do() -> None:
    tenant = Tenant.objects.create(name="Impact workspace")
    current = member(tenant, "current@example.com", WorkspaceRole.ADMIN)
    chosen = member(tenant, "chosen@example.com", WorkspaceRole.VIEWER)
    plant(tenant, current, "Automated park", automated=True)
    plant(tenant, current, "Manual park", automated=False)
    invitation(tenant, current, "open@example.com", open_=True)
    invitation(tenant, current, "expired@example.com", open_=False)
    invitation(tenant, current, "expired-admin@example.com", open_=False, role=WorkspaceRole.ADMIN)
    User.objects.filter(pk=current.pk).update(is_active=False)
    client, _ = verified_admin()
    url = reverse("admin:workspace_tenant_administrator", args=(tenant.pk,))

    page = client.get(url).content.decode()
    client.post(url, {"administrator": chosen.pk, "expected_admin_id": current.pk})

    impact = page[page.index('id="impact-title"') :]
    assert "current@example.com" in impact
    assert "Inactive account" in impact
    assert "New role of current@example.com: Member" in impact
    assert "Automated park" in impact
    assert "Manual park" not in impact
    assert "open@example.com" in impact
    assert "expired@example.com" not in impact
    assert "expired-admin@example.com" in impact
    assert (
        list(
            Plant.objects.filter(automatic_calibration_enabled=True).values_list("name", flat=True)
        )
        == []
    )
    revoked = dict(WorkspaceInvitation.objects.values_list("email", "revoked_reason"))
    assert revoked == {
        "open@example.com": InvitationRevocation.ADMINISTRATOR_CHANGED,
        "expired-admin@example.com": InvitationRevocation.ADMINISTRATOR_CHANGED,
        "expired@example.com": "",
    }


def test_a_workspace_without_an_administrator_shows_no_one_to_demote() -> None:
    tenant = Tenant.objects.create(name="Unadministered")
    member(tenant, "viewer@example.com", WorkspaceRole.VIEWER)
    client, _ = verified_admin()

    page = client.get(
        reverse("admin:workspace_tenant_administrator", args=(tenant.pk,))
    ).content.decode()

    impact = page[page.index('id="impact-title"') :]
    assert "New role of" not in impact
    assert "No plant uses automatic calibration." in impact
    assert "No invitation is open." in impact
