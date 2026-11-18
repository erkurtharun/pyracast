import pytest

from pyracast.accounts.models import Membership, Tenant, WorkspaceInvitation, WorkspaceRole
from pyracast.plants.models import Plant
from tests.support.browser import (
    Gateway,
    run_spec,
    seed_accounts,
    seed_forecast_days,
    seed_other_workspace,
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def test_a_platform_administrator_creates_a_workspace_with_a_plant_and_manages_members(
    web_gateway: Gateway,
) -> None:
    accounts = seed_accounts()
    seed_forecast_days(accounts.system)
    other = seed_other_workspace()

    run_spec(
        "admin-workspaces.spec.ts",
        web_gateway,
        BROWSER_CONSOLE_EMAIL=accounts.console.email,
        BROWSER_ADMIN_PASSWORD=accounts.password,
        BROWSER_CONSOLE_TOTP_SEED=accounts.console_totp,
        BROWSER_OTHER_TENANT=str(other.tenant.pk),
        BROWSER_REMOVABLE_MEMBER=other.removable.email,
    )

    console_tenant = Tenant.objects.get(name="Browser console customer")
    owner_invitation = WorkspaceInvitation.objects.get(tenant=console_tenant)
    assert (owner_invitation.email, owner_invitation.role, owner_invitation.created_by) == (
        "browser-owner@example.test",
        WorkspaceRole.ADMIN,
        accounts.console,
    )
    console_plant = Plant.objects.select_related("submission").get(region__tenant=console_tenant)
    assert console_plant.name == "Browser console plant"
    assert (console_plant.region.latitude, console_plant.region.longitude) == (41.0082, 28.9784)
    assert (console_plant.submission.surface_tilt, console_plant.submission.surface_azimuth) == (
        25,
        180,
    )
    assert not Membership.objects.filter(user=other.removable).exists()
    invited_viewer = WorkspaceInvitation.objects.get(
        tenant=other.tenant, email="browser-viewer@example.test"
    )
    assert invited_viewer.role == WorkspaceRole.VIEWER
