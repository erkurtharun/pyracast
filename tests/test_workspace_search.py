import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.platform_admin.tenants import tenant_list_context
from tests.support.api import verified_admin

pytestmark = pytest.mark.django_db


def workspace(name: str, *members: tuple[str, str, bool]) -> Tenant:
    tenant = Tenant.objects.create(name=name)
    for address, role, active in members:
        user = User.objects.create_user(username=address, email=address, is_active=active)
        Membership.objects.create(tenant=tenant, user=user, role=role)
    return tenant


def rows(query: str = "", needs_administrator: bool = False) -> dict[str, tuple[int, bool]]:
    page = tenant_list_context(query, "1", needs_administrator=needs_administrator)["page_obj"]
    return {tenant.name: (tenant.member_count, tenant.administrator_active) for tenant in page}


def test_a_member_address_finds_its_workspace_with_its_active_member_count() -> None:
    workspace(
        "Kayseri Solar",
        ("owner@kayseri.example", WorkspaceRole.ADMIN, True),
        ("ticket@kayseri.example", WorkspaceRole.VIEWER, True),
        ("left@kayseri.example", WorkspaceRole.VIEWER, False),
    )
    workspace("Other", ("someone@other.example", WorkspaceRole.ADMIN, True))

    assert rows("ticket@kayseri") == {"Kayseri Solar": (2, True)}
    assert rows("kayseri solar") == {"Kayseri Solar": (2, True)}


def test_workspaces_without_an_active_administrator_are_listed_apart() -> None:
    workspace("Run", ("admin@run.example", WorkspaceRole.ADMIN, True))
    workspace("Inactive", ("admin@inactive.example", WorkspaceRole.ADMIN, False))
    workspace("Empty", ("viewer@empty.example", WorkspaceRole.VIEWER, True))

    assert set(rows(needs_administrator=True)) == {"Inactive", "Empty"}
    assert rows()["Inactive"] == (0, False)


def test_the_list_marks_an_inactive_administrator_and_keeps_the_filter_in_its_links() -> None:
    workspace("Inactive", ("admin@inactive.example", WorkspaceRole.ADMIN, False))
    client, _ = verified_admin()

    page = client.get(reverse("admin:workspace_tenants"), {"needs_administrator": "1"})

    content = page.content.decode()
    assert page.status_code == 200
    assert "admin@inactive.example" in content
    assert "Inactive account" in content
    assert 'name="needs_administrator" value="1" checked' in content
