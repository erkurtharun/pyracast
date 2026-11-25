import json
import logging
from collections.abc import Callable

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.utils import timezone

from pyracast.accounts.audit import logger
from pyracast.accounts.invitations import accept_workspace_invitation, invite_workspace_member
from pyracast.accounts.models import (
    Membership,
    Tenant,
    UserLanguagePreference,
    WorkspaceAdminChange,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.accounts.operations import reassign_workspace_admin
from pyracast.errors import ConflictError, DomainError
from tests.support.concurrency import behind_open_transaction
from tests.support.outbox import deliver_mail
from tests.support.workspaces import invitation_token

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


@pytest.fixture
def unadministered() -> tuple[Tenant, User]:
    tenant = Tenant.objects.create(name="Console workspace")
    return tenant, User.objects.create_superuser(username="platform", email="platform@example.com")


def verified_account(username: str, email: str) -> User:
    user = User.objects.create_user(username=username, email=email)
    EmailAddress.objects.create(user=user, email=email, verified=True)
    return user


def pending(tenant: Tenant) -> list[tuple[str, str]]:
    return list(
        WorkspaceInvitation.objects.filter(
            tenant=tenant,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .order_by("pk")
        .values_list("email", "role")
    )


def test_only_a_platform_administrator_invites_an_administrator_and_only_one_stays_pending(
    unadministered: tuple[Tenant, User],
) -> None:
    tenant, platform = unadministered
    invite_workspace_member(platform, tenant.pk, "viewer@example.com", platform=True)
    first = invite_workspace_member(
        platform, tenant.pk, "first@example.com", role=WorkspaceRole.ADMIN, platform=True
    )
    assert (first.role, first.created_by) == (WorkspaceRole.ADMIN, platform)
    invite_workspace_member(
        platform, tenant.pk, "second@example.com", role=WorkspaceRole.ADMIN, platform=True
    )
    assert pending(tenant) == [
        ("viewer@example.com", WorkspaceRole.VIEWER),
        ("second@example.com", WorkspaceRole.ADMIN),
    ]

    administrator = User.objects.create_user(username="administrator")
    Membership.objects.create(tenant=tenant, user=administrator, role=WorkspaceRole.ADMIN)
    with pytest.raises(PermissionDenied):
        invite_workspace_member(
            administrator, tenant.pk, "third@example.com", role=WorkspaceRole.ADMIN
        )
    with pytest.raises(ConflictError) as refusal:
        invite_workspace_member(
            platform, tenant.pk, "third@example.com", role=WorkspaceRole.ADMIN, platform=True
        )
    assert refusal.value.code == "workspace_admin_changed"
    assert len(pending(tenant)) == 2


def test_accepting_an_administrator_invitation_makes_the_account_the_administrator(
    unadministered: tuple[Tenant, User],
) -> None:
    tenant, platform = unadministered
    invite_workspace_member(
        platform, tenant.pk, "owner@example.com", role=WorkspaceRole.ADMIN, platform=True
    )
    token = invitation_token()
    owner = verified_account("owner", "owner@example.com")
    client = Client()
    client.force_login(owner)
    accepted = client.post(
        "/api/invitations/accept/", {"token": token}, content_type="application/json"
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"tenant_id": tenant.pk, "role": "admin"}
    assert Membership.objects.get(tenant=tenant, user=owner).role == WorkspaceRole.ADMIN
    change = WorkspaceAdminChange.objects.get(tenant=tenant)
    assert (change.actor, change.administrator, change.previous_administrator) == (
        platform,
        owner,
        None,
    )


def test_accepting_an_administrator_invitation_raises_an_existing_viewer(
    unadministered: tuple[Tenant, User],
) -> None:
    tenant, platform = unadministered
    viewer = verified_account("viewer", "viewer@example.com")
    EmailAddress.objects.create(user=viewer, email="work@example.com", verified=True)
    Membership.objects.create(tenant=tenant, user=viewer)
    invite_workspace_member(
        platform, tenant.pk, "work@example.com", role=WorkspaceRole.ADMIN, platform=True
    )
    membership = accept_workspace_invitation(viewer, invitation_token())
    assert membership.role == WorkspaceRole.ADMIN
    assert Membership.objects.get(tenant=tenant, user=viewer).role == WorkspaceRole.ADMIN
    assert Membership.objects.filter(tenant=tenant).count() == 1
    assert WorkspaceAdminChange.objects.get(tenant=tenant).administrator == viewer


def test_an_administrator_invitation_is_unavailable_once_the_workspace_has_an_administrator(
    unadministered: tuple[Tenant, User],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    caplog.set_level(logging.INFO, logger="pyracast.security")
    tenant, platform = unadministered
    invite_workspace_member(
        platform, tenant.pk, "late@example.com", role=WorkspaceRole.ADMIN, platform=True
    )
    token = invitation_token()
    administrator = User.objects.create_user(username="administrator")
    Membership.objects.create(tenant=tenant, user=administrator, role=WorkspaceRole.ADMIN)
    late = verified_account("late", "late@example.com")
    with pytest.raises(DomainError) as refusal:
        accept_workspace_invitation(late, token)
    assert refusal.value.code == "invitation_unavailable"
    assert not Membership.objects.filter(user=late).exists()
    assert WorkspaceInvitation.objects.get().accepted_at is None
    assert not WorkspaceAdminChange.objects.exists()
    denied = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "pyracast.security"
        and json.loads(record.message)["event"] == "invitation_denied"
    ]
    assert denied == [
        {
            "event": "invitation_denied",
            "user_id": late.pk,
            "client_ip": None,
            "tenant_id": tenant.pk,
            "target_id": WorkspaceInvitation.objects.get().pk,
            "reason": "administrator_exists",
        }
    ]


def outcome(operation: Callable[[], object]) -> Callable[[], str]:
    def run() -> str:
        try:
            operation()
        except DomainError as refusal:
            return str(refusal.code)
        return "done"

    return run


def test_concurrent_administrator_invitations_leave_only_the_later_one_pending(
    unadministered: tuple[Tenant, User],
) -> None:
    tenant, platform = unadministered

    def invite(email: str) -> Callable[[], str]:
        return outcome(
            lambda: invite_workspace_member(
                platform, tenant.pk, email, role=WorkspaceRole.ADMIN, platform=True
            )
        )

    assert behind_open_transaction(invite("first@example.com"), invite("second@example.com")) == (
        "done",
        "done",
    )
    assert pending(tenant) == [("second@example.com", WorkspaceRole.ADMIN)]


def test_competing_acceptances_of_an_administrator_invitation_record_one_change(
    unadministered: tuple[Tenant, User],
) -> None:
    tenant, platform = unadministered
    invite_workspace_member(
        platform, tenant.pk, "owner@example.com", role=WorkspaceRole.ADMIN, platform=True
    )
    token = invitation_token()
    owner = verified_account("owner", "owner@example.com")
    accept = outcome(lambda: accept_workspace_invitation(owner, token))

    assert behind_open_transaction(accept, accept) == ("done", "done")
    assert Membership.objects.get(tenant=tenant).user == owner
    assert Membership.objects.get(tenant=tenant).role == WorkspaceRole.ADMIN
    assert WorkspaceAdminChange.objects.get(tenant=tenant).administrator == owner


@pytest.mark.parametrize(
    ("first", "refusal"),
    [("acceptance", "workspace_admin_changed"), ("reassignment", "invitation_unavailable")],
)
def test_administrator_acceptance_and_reassignment_leave_one_administrator(
    unadministered: tuple[Tenant, User], first: str, refusal: str
) -> None:
    tenant, platform = unadministered
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=tenant, user=viewer)
    invite_workspace_member(
        platform, tenant.pk, "owner@example.com", role=WorkspaceRole.ADMIN, platform=True
    )
    token = invitation_token()
    owner = verified_account("owner", "owner@example.com")
    operations = {
        "acceptance": outcome(lambda: accept_workspace_invitation(owner, token)),
        "reassignment": outcome(
            lambda: reassign_workspace_admin(platform, tenant.pk, viewer.pk, expected_admin_id=None)
        ),
    }
    second = "reassignment" if first == "acceptance" else "acceptance"

    assert behind_open_transaction(operations[first], operations[second]) == ("done", refusal)
    administrator = owner if first == "acceptance" else viewer
    assert list(
        Membership.objects.filter(tenant=tenant, role=WorkspaceRole.ADMIN).values_list(
            "user", flat=True
        )
    ) == [administrator.pk]
    assert WorkspaceAdminChange.objects.get(tenant=tenant).administrator == administrator
    assert pending(tenant) == []


def test_viewer_invitation_acceptance_keeps_the_viewer_role(
    unadministered: tuple[Tenant, User],
) -> None:
    tenant, platform = unadministered
    invite_workspace_member(platform, tenant.pk, "guest@example.com", platform=True)
    guest = verified_account("guest", "guest@example.com")
    assert accept_workspace_invitation(guest, invitation_token()).role == WorkspaceRole.VIEWER
    assert not WorkspaceAdminChange.objects.exists()


def test_invitation_mail_names_the_offered_role_in_the_requested_language(
    unadministered: tuple[Tenant, User],
) -> None:
    tenant, platform = unadministered
    UserLanguagePreference.objects.create(user=platform, language="tr")
    invite_workspace_member(
        platform,
        tenant.pk,
        "owner@example.com",
        role=WorkspaceRole.ADMIN,
        language="en",
        platform=True,
    )
    deliver_mail()
    message = mail.outbox[-1]
    assert message.subject == "Pyracast workspace invitation"
    assert "Invited role: Workspace administrator\n" in str(message.body)
    invite_workspace_member(
        platform,
        tenant.pk,
        "yonetici@example.com",
        role=WorkspaceRole.ADMIN,
        language="tr",
        platform=True,
    )
    deliver_mail()
    assert "Davet edildiğiniz rol: Çal\u0131şma alan\u0131 yöneticisi\n" in str(
        mail.outbox[-1].body
    )
    invite_workspace_member(platform, tenant.pk, "izleyici@example.com", platform=True)
    deliver_mail()
    assert "Davet edildiğiniz rol: Üye\n" in str(mail.outbox[-1].body)


def test_accepting_an_administrator_invitation_records_the_administrator_grant(
    unadministered: tuple[Tenant, User],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    caplog.set_level(logging.INFO, logger="pyracast.security")
    tenant, platform = unadministered
    invitation = invite_workspace_member(
        platform, tenant.pk, "owner@example.com", role=WorkspaceRole.ADMIN, platform=True
    )
    token = invitation_token()
    owner = verified_account("owner", "owner@example.com")
    caplog.clear()
    accept_workspace_invitation(owner, token)
    assert [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "pyracast.security"
    ] == [
        {
            "event": "workspace_admin_reassigned",
            "user_id": platform.pk,
            "client_ip": None,
            "tenant_id": tenant.pk,
            "target_id": owner.pk,
        },
        {
            "event": "invitation_accepted",
            "user_id": owner.pk,
            "client_ip": None,
            "tenant_id": tenant.pk,
            "target_id": invitation.pk,
        },
    ]
