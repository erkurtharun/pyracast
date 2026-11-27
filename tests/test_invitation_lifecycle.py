from datetime import timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from pyracast.accounts.invitations import (
    accept_workspace_invitation,
    invite_workspace_member,
    reissue_workspace_invitation,
)
from pyracast.accounts.models import Membership, Tenant, WorkspaceInvitation, WorkspaceRole
from pyracast.accounts.operations import reassign_workspace_admin, remove_workspace_member
from tests.support.api import verified_admin
from tests.support.company_registration import signup
from tests.support.outbox import deliver_mail
from tests.support.workspaces import (
    ACCEPT,
    ALREADY_ACCEPTED,
    PREVIEW,
    UNAVAILABLE,
    administered_workspace,
    invitation_token,
    post,
    signed_in,
    verified_account,
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])

GUEST = "guest@example.com"


@pytest.fixture
def workspace() -> tuple[Tenant, User]:
    return administered_workspace()


def invitations_url(tenant: Tenant) -> str:
    return f"/api/tenants/{tenant.pk}/invitations/"


def invitation_url(tenant: Tenant, invitation_id: int) -> str:
    return f"{invitations_url(tenant)}{invitation_id}/"


def aged(invitation: WorkspaceInvitation, age: timedelta = timedelta(minutes=2)) -> None:
    WorkspaceInvitation.objects.filter(pk=invitation.pk).update(created_at=timezone.now() - age)


def output(invitation: WorkspaceInvitation, state: str) -> dict[str, Any]:
    return {
        "id": invitation.pk,
        "email": invitation.email,
        "role": invitation.role,
        "state": state,
        "created_at": invitation.created_at.isoformat().replace("+00:00", "Z"),
        "expires_at": invitation.expires_at.isoformat().replace("+00:00", "Z"),
        "delivery_status": invitation.delivery_status,
    }


def test_the_state_follows_from_stored_facts_and_never_from_a_moved_deadline() -> None:
    now = timezone.now()
    ahead, behind = now + timedelta(days=1), now - timedelta(days=1)

    def state(**fields: Any) -> str:
        return WorkspaceInvitation(**{"expires_at": ahead, **fields}).state_at(now)

    assert state() == "pending"
    assert state(token_hash=None) == "pending"
    assert state(role=WorkspaceRole.ADMIN) == "pending"
    assert state(delivery_status="failed") == "pending"
    assert state(expires_at=now) == "expired"
    assert state(expires_at=behind) == "expired"
    assert state(accepted_at=behind) == "accepted"
    assert state(accepted_at=behind, expires_at=behind) == "accepted"
    for reason in ("cancelled", "member_removed", "administrator_changed"):
        assert state(revoked_at=behind, revoked_reason=reason) == "revoked"
        assert state(revoked_at=behind, revoked_reason=reason, expires_at=behind) == "revoked"
    assert state(revoked_at=behind, revoked_reason="superseded") == "superseded"
    assert state(revoked_at=behind, revoked_reason="superseded", expires_at=behind) == "superseded"
    assert state(revoked_at=behind) == "revoked"


def test_cancelling_keeps_the_deadline_and_stores_when_and_why(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    invitation = invite_workspace_member(administrator, tenant.pk, GUEST)
    before = timezone.now()

    response = signed_in(administrator).delete(invitation_url(tenant, invitation.pk))

    assert response.status_code == 204
    cancelled = WorkspaceInvitation.objects.get()
    assert cancelled.expires_at == invitation.expires_at
    assert cancelled.revoked_at is not None
    assert cancelled.revoked_at >= before
    assert cancelled.revoked_reason == "cancelled"
    assert cancelled.state_at(timezone.now()) == "revoked"


def test_a_second_cancellation_answers_alike_and_keeps_the_first_record(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    invitation = invite_workspace_member(administrator, tenant.pk, GUEST)
    client = signed_in(administrator)
    assert client.delete(invitation_url(tenant, invitation.pk)).status_code == 204
    first = WorkspaceInvitation.objects.values_list(
        "expires_at", "revoked_at", "revoked_reason"
    ).get()

    assert client.delete(invitation_url(tenant, invitation.pk)).status_code == 204

    assert (
        WorkspaceInvitation.objects.values_list("expires_at", "revoked_at", "revoked_reason").get()
        == first
    )


def test_cancelling_an_accepted_invitation_is_refused_and_changes_nothing(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    invite_workspace_member(administrator, tenant.pk, GUEST)
    guest = verified_account("guest", GUEST)
    accept_workspace_invitation(guest, invitation_token())
    accepted = WorkspaceInvitation.objects.get()

    response = signed_in(administrator).delete(invitation_url(tenant, accepted.pk))

    assert (response.status_code, response.json()) == (400, ALREADY_ACCEPTED)
    assert WorkspaceInvitation.objects.values_list("expires_at", "revoked_at").get() == (
        accepted.expires_at,
        None,
    )
    assert Membership.objects.get(tenant=tenant, user=guest).role == WorkspaceRole.VIEWER


def test_a_newer_link_supersedes_the_open_invitation_and_keeps_its_deadline(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    first = invite_workspace_member(administrator, tenant.pk, GUEST)
    aged(first)

    second = invite_workspace_member(administrator, tenant.pk, GUEST)

    now = timezone.now()
    replaced = WorkspaceInvitation.objects.get(pk=first.pk)
    assert replaced.expires_at == first.expires_at
    assert replaced.revoked_reason == "superseded"
    assert replaced.state_at(now) == "superseded"
    assert WorkspaceInvitation.objects.get(pk=second.pk).state_at(now) == "pending"


@pytest.mark.parametrize("change", ["member_removed", "administrator_changed"])
def test_removal_and_an_administrator_change_close_open_invitations_with_their_reason(
    workspace: tuple[Tenant, User], change: str
) -> None:
    tenant, administrator = workspace
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=tenant, user=viewer)
    EmailAddress.objects.create(user=viewer, email="alias@example.com", verified=True)
    invitation = invite_workspace_member(administrator, tenant.pk, "alias@example.com")

    if change == "member_removed":
        remove_workspace_member(administrator, tenant.pk, viewer.pk)
    else:
        platform = User.objects.create_superuser(username="platform")
        reassign_workspace_admin(platform, tenant.pk, viewer.pk, expected_admin_id=administrator.pk)

    closed = WorkspaceInvitation.objects.get(pk=invitation.pk)
    assert closed.expires_at == invitation.expires_at
    assert closed.revoked_at is not None
    assert (closed.revoked_reason, closed.state_at(timezone.now())) == (change, "revoked")


def test_an_administrator_change_also_closes_an_expired_administrator_invitation() -> None:
    tenant = Tenant.objects.create(name="Unattended workspace")
    member = verified_account("member", "member@example.com")
    Membership.objects.create(tenant=tenant, user=member)
    platform = User.objects.create_superuser(username="platform", email="platform@example.com")
    invitation = invite_workspace_member(
        platform, tenant.pk, "chief@example.com", role=WorkspaceRole.ADMIN, platform=True
    )
    WorkspaceInvitation.objects.filter(pk=invitation.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )

    reassign_workspace_admin(platform, tenant.pk, member.pk, expected_admin_id=None)

    closed = WorkspaceInvitation.objects.get(pk=invitation.pk)
    assert (closed.revoked_reason, closed.state_at(timezone.now())) == (
        "administrator_changed",
        "revoked",
    )
    resent = signed_in(member).post(f"{invitation_url(tenant, invitation.pk)}resend/")
    assert (resent.status_code, resent.json()) == (400, UNAVAILABLE)
    with pytest.raises(ValidationError) as refused:
        reissue_workspace_invitation(platform, tenant.pk, invitation.pk, platform=True)
    assert refused.value.code == "invitation_unavailable"
    assert not WorkspaceInvitation.objects.filter(tenant=tenant).open().exists()


def test_a_cancelled_invitation_stays_unusable_everywhere_while_its_deadline_lies_ahead(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    invitation = invite_workspace_member(administrator, tenant.pk, GUEST)
    token = invitation_token()
    administration = signed_in(administrator)
    assert administration.delete(invitation_url(tenant, invitation.pk)).status_code == 204
    assert WorkspaceInvitation.objects.get().expires_at > timezone.now()

    refused_signup = signup(
        Client(enforce_csrf_checks=True),
        {"company_name": "", "email": GUEST, "invitation_token": token},
    )
    assert not User.objects.filter(email=GUEST).exists()
    guest = verified_account("guest", GUEST)
    accepted = post(signed_in(guest), ACCEPT, {"token": token})
    previewed = post(Client(), PREVIEW, {"token": token})
    resent = administration.post(f"{invitation_url(tenant, invitation.pk)}resend/")

    assert refused_signup.status_code == 400
    assert (accepted.status_code, accepted.json()) == (400, UNAVAILABLE)
    assert (previewed.status_code, previewed.json()) == (400, UNAVAILABLE)
    assert (resent.status_code, resent.json()) == (400, UNAVAILABLE)
    assert administration.get(invitations_url(tenant)).json() == {"invitations": []}
    assert not Membership.objects.filter(user=guest).exists()


def test_administrators_list_the_open_invitations_with_their_state(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    pending = invite_workspace_member(administrator, tenant.pk, GUEST)
    expired = invite_workspace_member(administrator, tenant.pk, "late@example.com")
    WorkspaceInvitation.objects.filter(pk=expired.pk).update(
        expires_at=timezone.now() - timedelta(hours=1)
    )
    invite_workspace_member(administrator, tenant.pk, "joined@example.com")
    accept_workspace_invitation(
        verified_account("joined", "joined@example.com"), invitation_token()
    )
    cancelled = invite_workspace_member(administrator, tenant.pk, "cancelled@example.com")
    signed_in(administrator).delete(invitation_url(tenant, cancelled.pk))
    replaced = invite_workspace_member(administrator, tenant.pk, "again@example.com")
    aged(replaced)
    newest = invite_workspace_member(administrator, tenant.pk, "again@example.com")
    deliver_mail()

    response = signed_in(administrator).get(invitations_url(tenant))

    assert response.status_code == 200
    rows = {row.pk: row for row in WorkspaceInvitation.objects.all()}
    assert response.json() == {
        "invitations": [
            output(rows[newest.pk], "pending"),
            output(rows[expired.pk], "expired"),
            output(rows[pending.pk], "pending"),
        ]
    }


def test_send_again_replaces_the_link_and_keeps_the_address_and_role(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    invitation = invite_workspace_member(administrator, tenant.pk, GUEST)
    old_token = invitation_token()
    aged(invitation)
    client = signed_in(administrator)

    response = client.post(f"{invitation_url(tenant, invitation.pk)}resend/")

    assert response.status_code == 201
    replacement = WorkspaceInvitation.objects.exclude(pk=invitation.pk).get()
    assert response.json() == output(replacement, "pending")
    assert (replacement.email, replacement.role) == (GUEST, WorkspaceRole.VIEWER)
    assert WorkspaceInvitation.objects.get(pk=invitation.pk).state_at(timezone.now()) == (
        "superseded"
    )
    new_token = invitation_token()
    assert mail.outbox[-1].to == [GUEST]
    guest = signed_in(verified_account("guest", GUEST))
    assert (post(guest, ACCEPT, {"token": old_token}).json()) == UNAVAILABLE
    assert post(guest, ACCEPT, {"token": new_token}).json() == {
        "tenant_id": tenant.pk,
        "role": "viewer",
    }


def test_an_expired_invitation_can_be_sent_again(workspace: tuple[Tenant, User]) -> None:
    tenant, administrator = workspace
    invitation = invite_workspace_member(administrator, tenant.pk, GUEST)
    WorkspaceInvitation.objects.filter(pk=invitation.pk).update(
        created_at=timezone.now() - timedelta(days=8), expires_at=timezone.now()
    )

    response = signed_in(administrator).post(f"{invitation_url(tenant, invitation.pk)}resend/")

    assert response.status_code == 201
    assert response.json()["state"] == "pending"
    assert WorkspaceInvitation.objects.get(pk=invitation.pk).revoked_reason == "superseded"


def test_send_again_refuses_closed_and_foreign_invitations_and_honours_the_limits(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    client = signed_in(administrator)
    invite_workspace_member(administrator, tenant.pk, "joined@example.com")
    accept_workspace_invitation(
        verified_account("joined", "joined@example.com"), invitation_token()
    )
    accepted = WorkspaceInvitation.objects.get()
    other = Tenant.objects.create(name="Other workspace")
    owner = User.objects.create_user(username="owner", email="owner@example.com")
    Membership.objects.create(tenant=other, user=owner, role=WorkspaceRole.ADMIN)
    foreign = invite_workspace_member(owner, other.pk, "foreign@example.com")
    cancelled = invite_workspace_member(administrator, tenant.pk, "cancelled@example.com")
    client.delete(invitation_url(tenant, cancelled.pk))
    fresh = invite_workspace_member(administrator, tenant.pk, GUEST)

    answers = {
        "accepted": client.post(f"{invitation_url(tenant, accepted.pk)}resend/"),
        "cancelled": client.post(f"{invitation_url(tenant, cancelled.pk)}resend/"),
        "foreign_id": client.post(f"{invitation_url(tenant, foreign.pk)}resend/"),
        "foreign_workspace": client.post(f"{invitation_url(other, foreign.pk)}resend/"),
        "unknown": client.post(f"{invitation_url(tenant, 999999)}resend/"),
    }
    at_once = client.post(f"{invitation_url(tenant, fresh.pk)}resend/")

    assert {name: (answer.status_code, answer.json()) for name, answer in answers.items()} == {
        "accepted": (400, ALREADY_ACCEPTED),
        "cancelled": (400, UNAVAILABLE),
        "foreign_id": (400, UNAVAILABLE),
        "foreign_workspace": (
            403,
            {"code": "membership_required", "message": "You are not a member of this workspace."},
        ),
        "unknown": (400, UNAVAILABLE),
    }
    assert WorkspaceInvitation.objects.get(pk=foreign.pk).state_at(timezone.now()) == "pending"
    assert at_once.status_code == 429
    assert at_once.json()["code"] == "invitation_rate_limited"
    assert WorkspaceInvitation.objects.get(pk=fresh.pk).state_at(timezone.now()) == "pending"


def test_the_console_refuses_to_cancel_an_accepted_invitation() -> None:
    tenant, administrator = administered_workspace()
    invite_workspace_member(administrator, tenant.pk, GUEST)
    accept_workspace_invitation(verified_account("guest", GUEST), invitation_token())
    accepted = WorkspaceInvitation.objects.get()
    client, _ = verified_admin()
    base = f"/admin/workspaces/{tenant.pk}/"

    response = client.post(f"{base}invitations/{accepted.pk}/revocation/")

    assert response["Location"] == f"{base}#invitations-title"
    page = client.get(base).content.decode()
    assert "This invitation was already accepted. Reload the list." in page
    assert f"Invitation to {GUEST} revoked." not in page
    assert WorkspaceInvitation.objects.values_list("expires_at", "revoked_at").get() == (
        accepted.expires_at,
        None,
    )
