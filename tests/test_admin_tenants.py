from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.http import QueryDict
from django.test import Client

from pyracast.accounts.models import Membership, Tenant
from pyracast.admin_tenants import WorkspaceAdministratorForm, tenant_list_context
from tests.test_api import verified_admin

pytestmark = pytest.mark.django_db


def workspace_members() -> tuple[Tenant, User, User]:
    tenant = Tenant.objects.create(name="Solar workspace")
    owner = User.objects.create_user(username="owner", email="owner@example.com")
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=tenant, user=owner, role="admin")
    Membership.objects.create(tenant=tenant, user=viewer)
    return tenant, owner, viewer


@pytest.mark.parametrize("identity", ["anonymous", "viewer", "admin", "unverified_platform"])
def test_workspace_administration_requires_verified_platform_admin(identity: str) -> None:
    tenant, owner, viewer = workspace_members()
    client = Client()
    if identity != "anonymous":
        user = owner if identity == "admin" else viewer
        if identity == "unverified_platform":
            user.is_staff = user.is_superuser = True
            user.save()
        client.force_login(user)
    for path in ("/admin/workspaces/", f"/admin/workspaces/{tenant.pk}/"):
        assert client.get(path).status_code == 302
        assert client.post(path, {"administrator": viewer.pk}).status_code == 302
    assert Membership.objects.get(tenant=tenant, user=owner).role == "admin"


def test_workspace_admin_selects_only_active_members_and_transfers_permissions() -> None:
    tenant, owner, viewer = workspace_members()
    outsider = User.objects.create_user(username="outsider", email="outsider@example.com")
    inactive = User.objects.create_user(username="inactive", is_active=False)
    Membership.objects.create(tenant=tenant, user=inactive)
    client, _ = verified_admin()
    path = f"/admin/workspaces/{tenant.pk}/"
    page = client.get(path)
    assert page.status_code == 200
    content = page.content.decode()
    assert owner.email in content
    assert viewer.email in content
    assert outsider.email not in content
    assert f'value="{inactive.pk}"' not in content
    response = client.post(path, {"administrator": outsider.pk, "expected_admin_id": owner.pk})
    assert response.status_code == 400
    assert Membership.objects.get(tenant=tenant, user=owner).role == "admin"
    response = client.post(path, {"administrator": viewer.pk, "expected_admin_id": owner.pk})
    assert response.status_code == 302
    assert Membership.objects.get(tenant=tenant, user=owner).role == "viewer"
    assert Membership.objects.get(tenant=tenant, user=viewer).role == "admin"
    assert b"Workspace administrator updated." in client.get(response["Location"]).content
    response = client.post(path, {"administrator": owner.pk, "expected_admin_id": owner.pk})
    assert response.status_code == 400
    assert Membership.objects.get(tenant=tenant, user=viewer).role == "admin"


def test_workspace_admin_csrf_and_method_boundaries() -> None:
    tenant, owner, viewer = workspace_members()
    client, _ = verified_admin(enforce_csrf_checks=True)
    path = f"/admin/workspaces/{tenant.pk}/"
    assert client.post(path, {"administrator": viewer.pk}).status_code == 403
    assert Membership.objects.get(tenant=tenant, user=owner).role == "admin"
    assert client.get(path).status_code == 200
    token = client.cookies["csrftoken"].value
    assert client.put(path, HTTP_X_CSRFTOKEN=token).status_code == 405
    assert client.post("/admin/workspaces/", HTTP_X_CSRFTOKEN=token).status_code == 405
    assert client.get("/admin/workspaces/999999/").status_code == 404


def test_workspace_list_search_pagination_and_empty_administrator() -> None:
    tenant, owner, _ = workspace_members()
    client, _ = verified_admin()
    first = client.get("/admin/workspaces/")
    assert first.status_code == 200
    assert owner.email.encode() in first.content
    for index in range(20):
        Tenant.objects.create(name=f"Other workspace {index:02}")
    assert tenant_list_context("", "invalid")["page_obj"].number == 1
    assert tenant_list_context("", "999")["page_obj"].number == 2
    assert tenant_list_context(tenant.name, "1")["page_obj"].paginator.count == 1
    for page in ("1", "2"):
        assert client.get(f"/admin/workspaces/?page={page}").status_code == 200
    assert b"No workspaces found." in client.get("/admin/workspaces/?q=missing").content
    empty = Tenant.objects.get(name="Other workspace 00")
    assert client.get(f"/admin/workspaces/{empty.pk}/").status_code == 200


def test_workspace_choice_falls_back_to_username() -> None:
    tenant, owner, viewer = workspace_members()
    viewer.email = ""
    viewer.save(update_fields=("email",))
    form = WorkspaceAdministratorForm(tenant)
    assert "viewer" in str(form["administrator"])
    assert form.initial["expected_admin_id"] == owner.pk
    assert not WorkspaceAdministratorForm(tenant, QueryDict("administrator=")).is_valid()


def test_workspace_admin_form_handles_concurrent_membership_change(monkeypatch: Any) -> None:
    tenant, owner, viewer = workspace_members()
    client, _ = verified_admin()

    def removed(*_args: Any, **_kwargs: Any) -> None:
        raise ValidationError("The selected member is no longer active.")

    monkeypatch.setattr("pyracast.admin_tenants.reassign_workspace_admin", removed)
    response = client.post(
        f"/admin/workspaces/{tenant.pk}/",
        {"administrator": viewer.pk, "expected_admin_id": owner.pk},
    )
    assert response.status_code == 400
    assert b"The selected member is no longer active." in response.content
