from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
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
    WorkspaceRole,
)
from pyracast.accounts.operations import reassign_workspace_admin
from pyracast.errors import DomainError
from tests.support.company_registration import mailed_code, signup
from tests.support.outbox import deliver_mail
from tests.support.workspaces import invitation_token

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


@pytest.fixture
def workspace() -> tuple[Tenant, User, User]:
    tenant = Tenant.objects.create(name="Solar workspace")
    administrator = User.objects.create_user(username="workspace-admin", email="admin@example.com")
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=tenant, user=administrator, role=WorkspaceRole.ADMIN)
    Membership.objects.create(tenant=tenant, user=viewer)
    return tenant, administrator, viewer


def platform_admin() -> User:
    return User.objects.create_superuser(username="platform", email="platform@example.com")


def test_database_enforces_a_nonblank_workspace_name_and_single_admin(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, _, viewer = workspace
    assert connection.vendor == "postgresql"
    with pytest.raises(IntegrityError), transaction.atomic():
        Tenant.objects.create(name=" ")
    namesake = Tenant.objects.create(name=" SOLAR WORKSPACE ")
    renamed = Tenant.objects.create(name="Bay workspace")
    Tenant.objects.filter(pk=renamed.pk).update(name=tenant.name)
    assert Tenant.objects.filter(name=tenant.name).count() == 2
    assert Tenant.objects.get(pk=namesake.pk).name == " SOLAR WORKSPACE "
    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.filter(tenant=tenant, user=viewer).update(role="admin")
    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.filter(tenant=tenant, user=viewer).update(role="owner")


def test_platform_reassignment_requires_active_existing_member_and_current_selection(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, viewer = workspace
    platform = platform_admin()
    with pytest.raises(PermissionDenied):
        reassign_workspace_admin(
            administrator, tenant.pk, viewer.pk, expected_admin_id=administrator.pk
        )
    outsider = User.objects.create_user(username="outsider")
    with pytest.raises(ValidationError, match="active member"):
        reassign_workspace_admin(
            platform, tenant.pk, outsider.pk, expected_admin_id=administrator.pk
        )
    viewer.is_active = False
    viewer.save(update_fields=["is_active"])
    with pytest.raises(ValidationError, match="active member"):
        reassign_workspace_admin(platform, tenant.pk, viewer.pk, expected_admin_id=administrator.pk)
    viewer.is_active = True
    viewer.save(update_fields=["is_active"])
    reassign_workspace_admin(platform, tenant.pk, viewer.pk, expected_admin_id=administrator.pk)
    assert list(
        Membership.objects.filter(tenant=tenant, role="admin").values_list("user_id", flat=True)
    ) == [viewer.pk]
    assert WorkspaceAdminChange.objects.get().administrator == viewer
    with pytest.raises(ValidationError, match="changed"):
        reassign_workspace_admin(
            platform, tenant.pk, administrator.pk, expected_admin_id=administrator.pk
        )
    for rejected in (administrator, platform):
        with pytest.raises(PermissionDenied), transaction.atomic():
            tenant_membership(rejected, tenant.pk, role=WorkspaceRole.ADMIN, lock=True)
    with transaction.atomic():
        tenant_membership(viewer, tenant.pk, role=WorkspaceRole.ADMIN, lock=True)


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_invitation_requires_admin_and_verified_matching_email_and_is_single_use(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, viewer = workspace
    with pytest.raises(PermissionDenied):
        invite_workspace_member(viewer, tenant.pk, "new@example.com")
    invitation = invite_workspace_member(administrator, tenant.pk, " New@Example.COM ")
    token = invitation_token()
    assert invitation.email == "new@example.com"
    deliver_mail()
    assert f"/app/invitation/#token={token}" in str(mail.outbox[-1].body)
    deliver_mail()
    assert "?token=" not in str(mail.outbox[-1].body)
    invitation.refresh_from_db()
    assert invitation.token_hash == invitation_digest(token)
    assert token not in str(WorkspaceInvitation.objects.values().get())
    invited = User.objects.create_user(username="invited", email="new@example.com")
    with pytest.raises(PermissionDenied):
        accept_workspace_invitation(invited, token)
    EmailAddress.objects.create(user=viewer, email=viewer.email, verified=True)
    with pytest.raises(PermissionDenied):
        accept_workspace_invitation(viewer, token)
    address = EmailAddress.objects.create(user=invited, email="NEW@example.com", verified=True)
    invited.is_active = False
    invited.save(update_fields=["is_active"])
    with pytest.raises(PermissionDenied):
        accept_workspace_invitation(invited, token)
    invited.is_active = True
    invited.save(update_fields=["is_active"])
    membership = accept_workspace_invitation(invited, token)
    assert membership.role == "viewer"
    assert membership.tenant == tenant
    assert WorkspaceInvitation.objects.get().accepted_at is not None
    assert accept_workspace_invitation(invited, token) == membership
    address.delete()


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_reissuing_and_expiring_invitation_revokes_old_secret(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, _ = workspace
    invite_workspace_member(administrator, tenant.pk, "new@example.com")
    old = invitation_token()
    WorkspaceInvitation.objects.update(created_at=timezone.now() - timedelta(minutes=2))
    latest = invite_workspace_member(administrator, tenant.pk, "new@example.com")
    new = invitation_token()
    invited = User.objects.create_user(username="invited", email="new@example.com")
    EmailAddress.objects.create(user=invited, email=invited.email, verified=True)
    with pytest.raises(ValidationError, match="no longer valid"):
        accept_workspace_invitation(invited, old)
    WorkspaceInvitation.objects.filter(pk=latest.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    with pytest.raises(ValidationError, match="no longer valid"):
        accept_workspace_invitation(invited, new)


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_concurrent_invitation_acceptance_creates_one_viewer(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, _ = workspace
    invite_workspace_member(administrator, tenant.pk, "new@example.com")
    token = invitation_token()
    invited = User.objects.create_user(username="invited", email="new@example.com")
    EmailAddress.objects.create(user=invited, email=invited.email, verified=True)
    barrier = Barrier(2)

    def accept() -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return accept_workspace_invitation(invited, token).role
            except ValidationError:
                return "used"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: accept(), range(2)))
    assert results == ["viewer", "viewer"]
    assert Membership.objects.filter(tenant=tenant, user=invited).count() == 1


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_invited_signup_waits_for_verification_without_creating_workspace(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, _ = workspace
    invite_workspace_member(administrator, tenant.pk, "new@example.com")
    token = invitation_token()
    client = Client(enforce_csrf_checks=True)
    response = signup(
        client, {"company_name": "", "email": "new@example.com", "invitation_token": token}
    )
    assert response.status_code == 401
    user = User.objects.get(email="new@example.com")
    assert not Membership.objects.filter(user=user).exists()
    assert Tenant.objects.count() == 1
    csrf = client.get("/api/session/").json()["csrf_token"]
    assert (
        client.post(
            "/api/auth/browser/v1/auth/email/verify",
            {"key": mailed_code()},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code
        == 200
    )
    csrf = client.get("/api/session/").json()["csrf_token"]
    accepted = client.post(
        "/api/invitations/accept/",
        {"token": token},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"tenant_id": tenant.pk, "role": "viewer"}
    assert Tenant.objects.count() == 1


def test_a_signup_may_reuse_an_existing_workspace_name(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, viewer = workspace
    response = signup(
        Client(enforce_csrf_checks=True), {"company_name": f" {tenant.name.upper()} "}
    )
    assert response.status_code == 401
    owner = Membership.objects.get(user__email="owner@example.com")
    assert owner.tenant_id != tenant.pk
    assert (owner.tenant.name, owner.role) == (tenant.name.upper(), WorkspaceRole.ADMIN)
    assert set(Membership.objects.filter(tenant=tenant).values_list("user_id", flat=True)) == {
        administrator.pk,
        viewer.pk,
    }


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_workspace_invitation_api_is_scoped_and_never_returns_secrets(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, viewer = workspace
    client = Client(enforce_csrf_checks=True)
    client.force_login(administrator)
    members_url = f"/api/tenants/{tenant.pk}/members/"
    invitations_url = f"/api/tenants/{tenant.pk}/invitations/"
    members = client.get(members_url)
    assert members.status_code == 200
    assert members.json() == {
        "members": [
            {"id": administrator.pk, "email": administrator.email, "role": "admin"},
            {"id": viewer.pk, "email": viewer.email, "role": "viewer"},
        ],
        "plants_at_recipient_limit": 0,
    }
    csrf = client.get("/api/session/").json()["csrf_token"]
    assert (
        client.post(
            invitations_url, {"email": "new@example.com"}, content_type="application/json"
        ).status_code
        == 403
    )
    unknown = client.post(
        invitations_url,
        {"email": "new@example.com", "role": "admin"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert unknown.status_code == 400
    existing = client.post(
        invitations_url,
        {"email": viewer.email},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert existing.status_code == 400
    invited = client.post(
        invitations_url,
        {"email": "new@example.com"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert invited.status_code == 201
    assert set(invited.json()) == {
        "id",
        "email",
        "role",
        "state",
        "created_at",
        "expires_at",
        "delivery_status",
    }
    assert invited.json()["delivery_status"] == "pending"
    token = invitation_token()
    assert token not in invited.content.decode()
    assert client.get(invitations_url).json() == {
        "invitations": [{**invited.json(), "delivery_status": "sent"}]
    }
    assert (
        client.post(
            invitations_url,
            {"email": "new@example.com"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code
        == 429
    )
    invitation_id = invited.json()["id"]
    wrong_tenant = Tenant.objects.create(name="Another tenant")
    Membership.objects.create(tenant=wrong_tenant, user=administrator, role="admin")
    assert (
        client.delete(
            f"/api/tenants/{wrong_tenant.pk}/invitations/{invitation_id}/", HTTP_X_CSRFTOKEN=csrf
        ).status_code
        == 400
    )
    assert (
        client.delete(f"{invitations_url}{invitation_id}/", HTTP_X_CSRFTOKEN=csrf).status_code
        == 204
    )
    assert client.get(invitations_url).json() == {"invitations": []}
    assert (
        client.post(
            "/api/invitations/accept/",
            {"token": token},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/invitations/accept/",
            {"token": "invalid"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code
        == 400
    )
    client.force_login(viewer)
    assert client.get(members_url).status_code == 403
    assert client.get(invitations_url).status_code == 403
    with transaction.atomic(), pytest.raises(PermissionDenied):
        tenant_membership(
            administrator, tenant.pk + wrong_tenant.pk + 1, role=WorkspaceRole.ADMIN, lock=True
        )


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_transferring_admin_revokes_pending_grants_and_idempotent_assignment_preserves_them(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, viewer = workspace
    invite_workspace_member(administrator, tenant.pk, "new@example.com")
    token = invitation_token()
    platform = platform_admin()
    reassign_workspace_admin(
        platform, tenant.pk, administrator.pk, expected_admin_id=administrator.pk
    )
    assert not WorkspaceAdminChange.objects.exists()
    assert WorkspaceInvitation.objects.get().state_at(timezone.now()) == "pending"
    reassign_workspace_admin(platform, tenant.pk, viewer.pk, expected_admin_id=administrator.pk)
    new = User.objects.create_user(username="new", email="new@example.com")
    EmailAddress.objects.create(user=new, email=new.email, verified=True)
    with pytest.raises(ValidationError, match="no longer valid"):
        accept_workspace_invitation(new, token)


def test_invitation_email_failure_has_explicit_unknown_outcome(
    workspace: tuple[Tenant, User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, administrator, _ = workspace

    def fail(message: object, *, before_send: Callable[[], bool]) -> None:
        assert before_send()
        raise OSError("secret transport details")

    monkeypatch.setattr("pyracast.notifications.outbox.send", fail)
    invitation = invite_workspace_member(administrator, tenant.pk, "new@example.com")
    deliver_mail()
    assert invitation.delivery_status == "pending"
    assert WorkspaceInvitation.objects.get().delivery_status == "uncertain"


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_invitation_mail_uses_administrators_saved_language(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, _ = workspace
    UserLanguagePreference.objects.create(user=administrator, language="tr")
    invite_workspace_member(administrator, tenant.pk, "new@example.com")
    deliver_mail()
    assert mail.outbox[-1].subject == "Pyracast çalışma alanı daveti"  # noqa: RUF001 -- Turkish text.
    deliver_mail()
    assert "Davet edildiğiniz rol: Üye\n" in str(mail.outbox[-1].body)


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_invitation_without_preference_uses_request_language(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, _ = workspace
    client = Client(enforce_csrf_checks=True, HTTP_ACCEPT_LANGUAGE="tr")
    client.force_login(administrator)
    token = client.get("/api/session/").json()["csrf_token"]
    response = client.post(
        f"/api/tenants/{tenant.pk}/invitations/",
        {"email": "new@example.com"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 201
    deliver_mail()
    assert "Davet edildiğiniz rol: Üye\n" in str(mail.outbox[-1].body)


def test_workspace_invitation_hourly_limit_is_durable(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, _ = workspace
    WorkspaceInvitation.objects.bulk_create(
        [
            WorkspaceInvitation(
                tenant=tenant,
                created_by=administrator,
                email=f"guest-{number}@example.com",
                token_hash=f"{number:064x}",
                expires_at=timezone.now() + timedelta(days=1),
            )
            for number in range(20)
        ]
    )
    with pytest.raises(DomainError) as refusal:
        invite_workspace_member(administrator, tenant.pk, "another@example.com")
    assert (refusal.value.code, refusal.value.status) == ("invitation_rate_limited", 429)
    assert refusal.value.retry_after in {3599, 3600}
    assert WorkspaceInvitation.objects.count() == 20


def test_invalid_invitation_signup_creates_no_account(workspace: tuple[Tenant, User, User]) -> None:
    response = signup(
        Client(enforce_csrf_checks=True),
        {"email": "bad@example.com", "company_name": "", "invitation_token": "unknown"},
    )
    assert response.status_code == 400
    assert not User.objects.filter(email="bad@example.com").exists()


def test_concurrent_administrator_transfers_cannot_overwrite_new_assignment(
    workspace: tuple[Tenant, User, User],
) -> None:
    tenant, administrator, viewer = workspace
    another = User.objects.create_user(username="another")
    Membership.objects.create(tenant=tenant, user=another)
    platform = platform_admin()
    barrier = Barrier(2)

    def transfer(user_id: int) -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                reassign_workspace_admin(
                    platform, tenant.pk, user_id, expected_admin_id=administrator.pk
                )
                return "changed"
            except ValidationError:
                return "stale"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(transfer, (viewer.pk, another.pk)))
    assert sorted(statuses) == ["changed", "stale"]
    assert Membership.objects.filter(tenant=tenant, role="admin").count() == 1
    assert WorkspaceAdminChange.objects.count() == 1
