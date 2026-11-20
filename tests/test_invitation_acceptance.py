import json
import logging
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.utils import timezone

from pyracast.accounts.audit import logger
from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import (
    Membership,
    Tenant,
    WorkspaceAdminChange,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.accounts.operations import remove_workspace_member
from pyracast.accounts.processes import lock_account
from tests.support.concurrency import behind_open_transaction
from tests.support.workspaces import (
    ACCEPT,
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


@pytest.fixture
def security_log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> pytest.LogCaptureFixture:
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    caplog.set_level(logging.INFO, logger="pyracast.security")
    return caplog


def events(log: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [
        line
        for record in log.records
        if record.name == "pyracast.security"
        and (line := json.loads(record.getMessage()))["event"] == name
    ]


def answer(response: Any) -> tuple[int, Any]:
    return response.status_code, response.json()


def test_an_acceptance_retried_after_a_lost_response_answers_as_the_first(
    workspace: tuple[Tenant, User], security_log: pytest.LogCaptureFixture
) -> None:
    tenant, administrator = workspace
    invite_workspace_member(administrator, tenant.pk, GUEST)
    token = invitation_token()
    client = signed_in(verified_account("guest", GUEST))

    first = post(client, ACCEPT, {"token": token})
    retried = post(client, ACCEPT, {"token": token})

    assert answer(first) == (200, {"tenant_id": tenant.pk, "role": "viewer"})
    assert answer(retried) == answer(first)
    guest = User.objects.get(username="guest")
    assert Membership.objects.filter(tenant=tenant, user=guest).count() == 1
    invitation = WorkspaceInvitation.objects.get()
    assert invitation.accepted_by == guest
    assert len(events(security_log, "invitation_accepted")) == 1
    assert token not in security_log.text


def test_an_accepted_invitation_is_unusable_for_every_other_account_and_a_removed_acceptor(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    invite_workspace_member(administrator, tenant.pk, GUEST)
    token = invitation_token()
    guest = verified_account("guest", GUEST)
    assert post(signed_in(guest), ACCEPT, {"token": token}).status_code == 200
    other = verified_account("other", "other@example.com")

    by_other = post(signed_in(other), ACCEPT, {"token": token})
    remove_workspace_member(administrator, tenant.pk, guest.pk)
    after_removal = post(signed_in(guest), ACCEPT, {"token": token})

    assert answer(by_other) == (400, UNAVAILABLE)
    assert answer(after_removal) == (400, UNAVAILABLE)
    assert not Membership.objects.filter(tenant=tenant, user__in=(guest, other)).exists()


@pytest.mark.parametrize("role", [WorkspaceRole.ADMIN, WorkspaceRole.VIEWER])
def test_an_existing_member_accepting_keeps_the_membership_and_its_role(
    workspace: tuple[Tenant, User], role: WorkspaceRole
) -> None:
    tenant, administrator = workspace
    if role == WorkspaceRole.ADMIN:
        member = administrator
    else:
        member = User.objects.create_user(username="viewer", email="viewer@example.com")
        Membership.objects.create(tenant=tenant, user=member)
    EmailAddress.objects.create(user=member, email="alias@example.com", verified=True)
    membership = Membership.objects.get(tenant=tenant, user=member)
    invite_workspace_member(administrator, tenant.pk, "alias@example.com")

    accepted = post(signed_in(member), ACCEPT, {"token": invitation_token()})

    assert answer(accepted) == (200, {"tenant_id": tenant.pk, "role": role})
    assert list(
        Membership.objects.filter(tenant=tenant, user=member).values_list("pk", "role")
    ) == [(membership.pk, role)]
    assert not WorkspaceAdminChange.objects.exists()
    assert WorkspaceInvitation.objects.get().accepted_by == member


def test_two_clients_accepting_one_invitation_at_once_both_get_the_first_answer(
    workspace: tuple[Tenant, User], security_log: pytest.LogCaptureFixture
) -> None:
    tenant, administrator = workspace
    invite_workspace_member(administrator, tenant.pk, GUEST)
    token = invitation_token()
    guest = verified_account("guest", GUEST)
    first, second = signed_in(guest), signed_in(guest)

    answers = behind_open_transaction(
        lambda: post(first, ACCEPT, {"token": token}),
        lambda: post(second, ACCEPT, {"token": token}),
    )

    assert [answer(response) for response in answers] == [
        (200, {"tenant_id": tenant.pk, "role": "viewer"})
    ] * 2
    assert Membership.objects.filter(tenant=tenant, user=guest).count() == 1
    assert len(events(security_log, "invitation_accepted")) == 1


def test_an_acceptance_decides_on_the_addresses_a_concurrent_account_change_leaves(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    invite_workspace_member(administrator, tenant.pk, GUEST)
    token = invitation_token()
    guest = verified_account("guest", GUEST)
    client = signed_in(guest)

    def drop_the_invited_address() -> None:
        lock_account(guest.pk)
        EmailAddress.objects.filter(user=guest, email=GUEST).delete()

    _, accepted = behind_open_transaction(
        drop_the_invited_address, lambda: post(client, ACCEPT, {"token": token})
    )

    assert answer(accepted) == (
        403,
        {
            "code": "invitation_email_unverified",
            "message": "Verify the invited email address before accepting.",
        },
    )
    assert not Membership.objects.filter(tenant=tenant, user=guest).exists()
    assert WorkspaceInvitation.objects.get().state_at(timezone.now()) == "pending"
