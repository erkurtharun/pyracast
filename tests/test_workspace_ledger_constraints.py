from datetime import timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from pyracast.accounts.invitations import (
    accept_workspace_invitation,
    invitation_digest,
    invite_workspace_member,
    revoke_workspace_invitation,
)
from pyracast.accounts.models import (
    Membership,
    Tenant,
    WorkspaceAdminChange,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.accounts.operations import reassign_workspace_admin, remove_workspace_member
from pyracast.db import violated_constraint
from tests.support.workspaces import invitation_token

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(name="workspace")
def administered_workspace() -> tuple[Tenant, User]:
    tenant = Tenant.objects.create(name="Ledger workspace")
    administrator = User.objects.create_user(username="ledger-admin", email="admin@example.com")
    Membership.objects.create(tenant=tenant, user=administrator, role=WorkspaceRole.ADMIN)
    return tenant, administrator


def stored_invitation(
    tenant: Tenant, actor: User, email: str, **values: Any
) -> WorkspaceInvitation:
    return WorkspaceInvitation.objects.create(
        tenant=tenant,
        created_by=actor,
        email=email,
        token_hash=invitation_digest(f"{email}-{WorkspaceInvitation.objects.count()}"),
        expires_at=timezone.now() + timedelta(days=1),
        **values,
    )


def refusal(write: Any) -> str | None:
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        write()
    return violated_constraint(raised.value)


def test_revoking_an_invitation_records_the_revocation_and_keeps_the_deadline(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    invitation = invite_workspace_member(administrator, tenant.pk, "guest@example.com")

    revoke_workspace_invitation(administrator, tenant.pk, invitation.pk)

    deadline = invitation.expires_at
    invitation.refresh_from_db()
    assert invitation.revoked_at is not None
    assert (invitation.expires_at, invitation.revoked_reason) == (deadline, "cancelled")
    assert invitation.accepted_at is None


def test_inviting_an_address_again_revokes_its_earlier_invitation_even_after_it_expired(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    earlier = invite_workspace_member(administrator, tenant.pk, "guest@example.com")
    deadline = timezone.now() - timedelta(days=1)
    WorkspaceInvitation.objects.filter(pk=earlier.pk).update(
        created_at=deadline - timedelta(days=7), expires_at=deadline
    )

    later = invite_workspace_member(administrator, tenant.pk, "guest@example.com")

    earlier.refresh_from_db()
    assert earlier.revoked_at is not None
    assert earlier.expires_at == deadline
    assert later.revoked_at is None


def test_an_administrator_invitation_revokes_the_open_administrator_invitation() -> None:
    tenant = Tenant.objects.create(name="Unadministered workspace")
    platform = User.objects.create_superuser(username="platform", email="platform@example.com")
    first = invite_workspace_member(
        platform, tenant.pk, "first@example.com", role=WorkspaceRole.ADMIN, platform=True
    )
    second = invite_workspace_member(
        platform, tenant.pk, "second@example.com", role=WorkspaceRole.ADMIN, platform=True
    )

    first.refresh_from_db()
    assert first.revoked_at is not None
    assert second.revoked_at is None


def test_an_accepted_invitation_is_never_marked_revoked(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    invite_workspace_member(administrator, tenant.pk, "guest@example.com")
    guest = User.objects.create_user(username="guest", email="guest@example.com")
    EmailAddress.objects.create(user=guest, email=guest.email, verified=True)
    accept_workspace_invitation(guest, invitation_token())
    invitation = WorkspaceInvitation.objects.get()

    with pytest.raises(ValidationError, match="no longer valid"):
        revoke_workspace_invitation(administrator, tenant.pk, invitation.pk)

    invitation.refresh_from_db()
    assert invitation.accepted_at is not None
    assert invitation.revoked_at is None


def test_removing_a_member_revokes_the_open_invitations_of_the_member_addresses(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=tenant, user=viewer)
    invitation = stored_invitation(tenant, administrator, viewer.email)
    other = stored_invitation(tenant, administrator, "other@example.com")

    remove_workspace_member(administrator, tenant.pk, viewer.pk)

    invitation.refresh_from_db()
    other.refresh_from_db()
    assert invitation.revoked_at is not None
    assert invitation.revoked_reason == "member_removed"
    assert invitation.expires_at > invitation.revoked_at
    assert other.revoked_at is None


def test_reassigning_the_administrator_revokes_open_invitations_and_records_both_accounts(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=tenant, user=viewer)
    invitation = stored_invitation(tenant, administrator, "guest@example.com")
    platform = User.objects.create_superuser(username="platform", email="platform@example.com")

    reassign_workspace_admin(platform, tenant.pk, viewer.pk, expected_admin_id=administrator.pk)

    invitation.refresh_from_db()
    assert invitation.revoked_at is not None
    change = WorkspaceAdminChange.objects.get()
    assert (change.administrator_id, change.previous_administrator_id) == (
        viewer.pk,
        administrator.pk,
    )


def test_the_database_refuses_an_administrator_change_that_names_one_account_twice(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    platform = User.objects.create_superuser(username="platform", email="platform@example.com")

    assert (
        refusal(
            lambda: WorkspaceAdminChange.objects.create(
                tenant=tenant,
                actor=platform,
                administrator=administrator,
                previous_administrator=administrator,
            )
        )
        == "workspaceadminchange_administrator_chk"
    )
    WorkspaceAdminChange.objects.create(tenant=tenant, actor=platform, administrator=administrator)


@pytest.mark.parametrize(
    ("change", "constraint"),
    [
        (
            lambda now: {"accepted_at": now, "revoked_at": now},
            "workspaceinvitation_revoked_at_chk",
        ),
        (
            lambda now: {"revoked_reason": "cancelled"},
            "workspaceinvitation_revoked_at_chk",
        ),
        (
            lambda now: {"accepted_by": User.objects.get(username="ledger-admin")},
            "workspaceinvitation_accepted_by_chk",
        ),
    ],
    ids=["accepted_and_revoked", "reason_without_revocation", "acceptor_without_acceptance"],
)
def test_the_database_refuses_an_invitation_state_no_writer_stores(
    change: Any, constraint: str, workspace: tuple[Tenant, User]
) -> None:
    tenant, administrator = workspace
    invitation = stored_invitation(tenant, administrator, "guest@example.com")
    rows = WorkspaceInvitation.objects.filter(pk=invitation.pk)

    assert refusal(lambda: rows.update(**change(timezone.now()))) == constraint


@pytest.mark.parametrize(
    ("first", "second", "constraint"),
    [
        (
            ("guest@example.com", WorkspaceRole.VIEWER),
            ("guest@example.com", WorkspaceRole.VIEWER),
            "workspaceinvitation_tenant_email_uniq",
        ),
        (
            ("first@example.com", WorkspaceRole.ADMIN),
            ("second@example.com", WorkspaceRole.ADMIN),
            "workspaceinvitation_tenant_administrator_uniq",
        ),
    ],
    ids=["address", "administrator"],
)
def test_the_database_keeps_one_open_invitation_per_address_and_one_for_the_administrator(
    first: tuple[str, str],
    second: tuple[str, str],
    constraint: str,
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    stored_invitation(tenant, administrator, first[0], role=first[1])

    assert (
        refusal(lambda: stored_invitation(tenant, administrator, second[0], role=second[1]))
        == constraint
    )
    other = Tenant.objects.create(name="Other workspace")
    stored_invitation(other, administrator, second[0], role=second[1])


@pytest.mark.parametrize(
    "closed",
    [
        lambda now: {"accepted_at": now},
        lambda now: {"revoked_at": now, "expires_at": now},
    ],
    ids=["accepted", "revoked"],
)
def test_a_closed_invitation_leaves_its_address_free_for_a_new_one(
    closed: Any, workspace: tuple[Tenant, User]
) -> None:
    tenant, administrator = workspace
    invitation = stored_invitation(
        tenant, administrator, "guest@example.com", role=WorkspaceRole.ADMIN
    )
    WorkspaceInvitation.objects.filter(pk=invitation.pk).update(**closed(timezone.now()))

    stored_invitation(tenant, administrator, "guest@example.com", role=WorkspaceRole.ADMIN)

    assert WorkspaceInvitation.objects.filter(tenant=tenant, email="guest@example.com").count() == 2
