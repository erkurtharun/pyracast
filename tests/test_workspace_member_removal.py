from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection, connections, transaction
from django.test import Client, override_settings
from django.utils import timezone

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.invitations import (
    accept_workspace_invitation,
    invitation_digest,
    invite_workspace_member,
)
from pyracast.accounts.models import (
    Membership,
    Tenant,
    UserLanguagePreference,
    WorkspaceAdminChange,
    WorkspaceInvitation,
    WorkspaceMemberRemoval,
)
from pyracast.accounts.operations import reassign_workspace_admin, remove_workspace_member
from pyracast.plants.models import Plant
from pyracast.plants.operations import create_plant
from pyracast.regions.models import AuditEvent, Region
from tests.test_domain import make_system
from tests.test_workspaces import invitation_token

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(name="workspace")
def removal_workspace() -> tuple[Tenant, User, User]:
    tenant = Tenant.objects.create(name="Solar workspace")
    administrator = User.objects.create_user(username="workspace-admin", email="admin@example.com")
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=tenant, user=administrator, role=Membership.Role.ADMIN)
    Membership.objects.create(tenant=tenant, user=viewer)
    return tenant, administrator, viewer


def outstanding_invitation(
    tenant: Tenant, actor: User, email: str, token: str
) -> WorkspaceInvitation:
    return WorkspaceInvitation.objects.create(
        tenant=tenant,
        created_by=actor,
        email=email,
        token_hash=invitation_digest(token),
        expires_at=timezone.now() + timedelta(days=1),
    )


def test_removal_revokes_access_in_existing_session_and_preserves_account_and_resources() -> None:
    system = make_system()
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=1
    )
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=system.tenant, user=viewer)
    other = Tenant.objects.create(name="Other")
    Membership.objects.create(tenant=other, user=viewer, role="admin")
    preference = UserLanguagePreference.objects.create(user=viewer, language="tr")
    viewer_client = Client()
    viewer_client.force_login(viewer)
    base = f"/api/tenants/{system.tenant.pk}/"
    region_url = f"{base}regions/{system.region.pk}/"
    location_url = f"{region_url}location-change-requests/"
    assert viewer_client.get(location_url).status_code == 200
    before = (Region.objects.count(), Plant.objects.count(), AuditEvent.objects.count())
    client = Client(enforce_csrf_checks=True)
    client.force_login(system.member)
    url = f"{base}members/{viewer.pk}/"
    assert client.delete(url).status_code == 403
    csrf = client.get("/api/session/").json()["csrf_token"]
    assert client.delete(url, HTTP_X_CSRFTOKEN=csrf).status_code == 204
    assert client.delete(url, HTTP_X_CSRFTOKEN=csrf).status_code == 204
    assert viewer_client.get(location_url).status_code == 403
    assert viewer_client.get(f"{base}sites/").status_code == 403
    assert viewer_client.delete(region_url).status_code == 403
    assert viewer_client.get("/api/session/").json()["user"]["id"] == viewer.pk
    assert [row["id"] for row in viewer_client.get("/api/tenants/").json()["results"]] == [other.pk]
    assert viewer_client.get(f"/api/tenants/{other.pk}/members/").status_code == 200
    assert User.objects.get(pk=viewer.pk).is_active
    assert UserLanguagePreference.objects.get(pk=preference.pk).language == "tr"
    assert (Region.objects.count(), Plant.objects.count(), AuditEvent.objects.count()) == before
    assert Plant.objects.get(pk=plant.pk).region_id == system.region.pk
    audit = WorkspaceMemberRemoval.objects.get()
    assert (audit.actor_id, audit.user_id, audit.tenant_id) == (
        system.member.pk,
        viewer.pk,
        system.tenant.pk,
    )


def test_removal_rejects_other_actors_and_admin_target_and_scopes_target(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, viewer = workspace
    outsider = User.objects.create_user(username="outsider")
    platform = User.objects.create_superuser(username="platform")
    other = Tenant.objects.create(name="Other")
    Membership.objects.create(tenant=other, user=outsider, role="admin")
    for actor in (viewer, outsider, platform):
        with pytest.raises(PermissionDenied):
            remove_workspace_member(actor, tenant.pk, viewer.pk)
    client = Client()
    for actor in (viewer, outsider, platform):
        client.force_login(actor)
        assert client.delete(f"/api/tenants/{tenant.pk}/members/{viewer.pk}/").status_code == 403
    client.force_login(administrator)
    assert client.delete(f"/api/tenants/{tenant.pk}/members/{administrator.pk}/").status_code == 400
    assert client.delete(f"/api/tenants/{tenant.pk}/members/{outsider.pk}/").status_code == 204
    assert client.delete(f"/api/tenants/{tenant.pk}/members/999999/").status_code == 204
    administrator.is_active = False
    administrator.save(update_fields=["is_active"])
    with pytest.raises(PermissionDenied):
        remove_workspace_member(administrator, tenant.pk, viewer.pk)
    assert Membership.objects.count() == 3
    assert not WorkspaceMemberRemoval.objects.exists()


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_removal_expires_known_email_invitations_but_allows_intentional_new_invite(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, viewer = workspace
    EmailAddress.objects.create(user=viewer, email="ALIAS@example.com", verified=True)
    EmailAddress.objects.create(user=viewer, email="unverified@example.com", verified=False)
    other = Tenant.objects.create(name="Other")
    invitations = [
        outstanding_invitation(tenant, administrator, email, f"old-{index}")
        for index, email in enumerate((viewer.email, "Alias@Example.com", "unverified@example.com"))
    ]
    unrelated = outstanding_invitation(tenant, administrator, "another@example.com", "unrelated")
    foreign = outstanding_invitation(other, administrator, "alias@example.com", "other")
    remove_workspace_member(administrator, tenant.pk, viewer.pk)
    for index, invitation in enumerate(invitations):
        invitation.refresh_from_db()
        assert invitation.expires_at <= timezone.now()
        with pytest.raises(ValidationError, match="invalid or expired"):
            accept_workspace_invitation(viewer, f"old-{index}")
    unrelated.refresh_from_db()
    foreign.refresh_from_db()
    assert unrelated.expires_at > timezone.now()
    assert foreign.expires_at > timezone.now()
    WorkspaceInvitation.objects.filter(tenant=tenant).update(
        created_at=timezone.now() - timedelta(minutes=2)
    )
    invite_workspace_member(administrator, tenant.pk, "alias@example.com")
    assert accept_workspace_invitation(viewer, invitation_token()).role == "viewer"
    assert WorkspaceMemberRemoval.objects.count() == 1


def test_removal_rolls_back_when_audit_cannot_be_saved(
    workspace: tuple[Tenant, User, User], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, administrator, viewer = workspace
    EmailAddress.objects.create(user=viewer, email=viewer.email, verified=True)
    invitation = outstanding_invitation(tenant, administrator, viewer.email, "old")

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(WorkspaceMemberRemoval, "save", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        remove_workspace_member(administrator, tenant.pk, viewer.pk)
    invitation.refresh_from_db()
    assert invitation.expires_at > timezone.now()
    assert Membership.objects.filter(tenant=tenant, user=viewer).exists()


def test_concurrent_removal_is_idempotent(workspace: tuple[Tenant, User, User]) -> None:
    tenant, administrator, viewer = workspace
    assert connection.vendor == "postgresql"
    barrier = Barrier(2)

    def remove() -> None:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            remove_workspace_member(administrator, tenant.pk, viewer.pk)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(remove) for _ in range(2)]
        for future in futures:
            future.result(timeout=15)
    assert not Membership.objects.filter(tenant=tenant, user=viewer).exists()
    assert WorkspaceMemberRemoval.objects.count() == 1


@pytest.mark.parametrize(
    ("first", "second"),
    [("remove", "transfer"), ("remove", "accept"), ("transfer", "remove"), ("accept", "remove")],
)
def test_removal_serializes_with_transfer_and_invitation_acceptance(
    workspace: tuple[Tenant, User, User], first: str, second: str
) -> None:
    tenant, administrator, viewer = workspace
    platform = User.objects.create_superuser(username="platform")
    EmailAddress.objects.create(user=viewer, email=viewer.email, verified=True)
    outstanding_invitation(tenant, administrator, viewer.email, "pending")
    first_finished, second_started = Event(), Event()

    def operation(name: str) -> str:
        try:
            if name == "remove":
                remove_workspace_member(administrator, tenant.pk, viewer.pk)
            elif name == "transfer":
                reassign_workspace_admin(
                    platform, tenant.pk, viewer.pk, expected_admin_id=administrator.pk
                )
            else:
                accept_workspace_invitation(viewer, "pending")
        except PermissionDenied, ValidationError:
            return "rejected"
        return "done"

    def run_first() -> str:
        close_old_connections()
        try:
            with transaction.atomic():
                Tenant.objects.select_for_update().get(pk=tenant.pk)
                result = operation(first)
                first_finished.set()
                assert second_started.wait(timeout=10)
                return result
        finally:
            connections.close_all()

    def run_second() -> str:
        close_old_connections()
        try:
            assert first_finished.wait(timeout=10)
            second_started.set()
            return operation(second)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        initial, following = pool.submit(run_first), pool.submit(run_second)
        assert initial.result(timeout=15) == "done"
        assert following.result(timeout=15) == ("done" if first == "accept" else "rejected")
    if first == "transfer":
        assert Membership.objects.get(tenant=tenant, user=viewer).role == "admin"
        assert WorkspaceAdminChange.objects.count() == 1
        assert not WorkspaceMemberRemoval.objects.exists()
        with pytest.raises(PermissionDenied):
            tenant_membership(administrator, tenant.pk, role=Membership.Role.ADMIN)
    else:
        assert not Membership.objects.filter(tenant=tenant, user=viewer).exists()
        assert WorkspaceMemberRemoval.objects.count() == 1
        assert Membership.objects.get(tenant=tenant, user=administrator).role == "admin"
