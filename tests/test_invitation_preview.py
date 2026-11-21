from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from pyracast.accounts.invitations import (
    accept_workspace_invitation,
    compose_invitation,
    invite_workspace_member,
)
from pyracast.accounts.models import Membership, Tenant, WorkspaceInvitation, WorkspaceRole
from pyracast.accounts.operations import remove_workspace_member
from tests.support.workspaces import (
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


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def preview(body: object, client: Client | None = None) -> Any:
    return post(client or Client(), PREVIEW, body)


def test_the_preview_names_the_workspace_role_inviter_address_and_deadline(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    invitation = invite_workspace_member(administrator, tenant.pk, GUEST)
    token = invitation_token()

    response = preview({"token": token})

    assert response.status_code == 200
    body = response.json()
    assert instant(body.pop("expires_at")) == invitation.expires_at
    assert body == {
        "workspace": "Solar workspace",
        "role": "viewer",
        "inviter": "admin@example.com",
        "email": GUEST,
    }
    assert WorkspaceInvitation.objects.get().state_at(timezone.now()) == "pending"


def test_the_preview_names_an_inviter_only_while_an_active_member_of_the_workspace(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    platform = User.objects.create_superuser(username="platform", email="platform@example.com")
    invite_workspace_member(administrator, tenant.pk, GUEST)
    by_administrator = invitation_token()
    invite_workspace_member(
        platform, tenant.pk, "console@example.com", role=WorkspaceRole.VIEWER, platform=True
    )
    by_console = invitation_token()

    named = preview({"token": by_administrator}).json()["inviter"]
    User.objects.filter(pk=administrator.pk).update(is_active=False)
    inactive = preview({"token": by_administrator}).json()["inviter"]
    console = preview({"token": by_console}).json()

    assert (named, inactive) == ("admin@example.com", None)
    assert (console["inviter"], console["email"]) == (None, "console@example.com")


def test_the_preview_does_not_tell_whether_the_address_has_an_account(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    invite_workspace_member(administrator, tenant.pk, GUEST)
    token = invitation_token()

    before = preview({"token": token}).json()
    verified_account("guest", GUEST)
    after = preview({"token": token}).json()

    assert before == after
    assert set(after) == {"workspace", "role", "inviter", "email", "expires_at"}


UNUSABLE = (
    "unknown",
    "empty",
    "too_long",
    "not_text",
    "missing",
    "cancelled",
    "expired",
    "superseded",
    "replaced_link",
    "accepted",
    "removed_acceptor",
)


def unusable_body(kind: str, tenant: Tenant, administrator: User) -> tuple[object, User]:
    someone = verified_account("someone", "someone@example.com")
    shapes: dict[str, object] = {
        "unknown": {"token": "x" * 43},
        "empty": {"token": ""},
        "too_long": {"token": "x" * 4096},
        "not_text": {"token": ["x"]},
        "missing": {},
    }
    if kind in shapes:
        return shapes[kind], someone
    invitation = invite_workspace_member(administrator, tenant.pk, GUEST)
    token = invitation_token()
    closing: dict[str, Callable[[], object]] = {
        "cancelled": lambda: signed_in(administrator).delete(
            f"/api/tenants/{tenant.pk}/invitations/{invitation.pk}/"
        ),
        "expired": lambda: WorkspaceInvitation.objects.filter(pk=invitation.pk).update(
            expires_at=timezone.now()
        ),
        "superseded": lambda: (
            WorkspaceInvitation.objects.update(created_at=timezone.now() - timedelta(minutes=2)),
            invite_workspace_member(administrator, tenant.pk, GUEST),
        ),
        "replaced_link": lambda: _mint_again(invitation.pk),
        "accepted": lambda: accept_workspace_invitation(verified_account("guest", GUEST), token),
        "removed_acceptor": lambda: _accept_and_remove(tenant, administrator, token),
    }
    closed = closing[kind]()
    return {"token": token}, closed if isinstance(closed, User) else someone


def _accept_and_remove(tenant: Tenant, administrator: User, token: str) -> User:
    guest = verified_account("guest", GUEST)
    accept_workspace_invitation(guest, token)
    remove_workspace_member(administrator, tenant.pk, guest.pk)
    return guest


def _mint_again(invitation_id: int) -> None:
    with transaction.atomic():
        compose_invitation(invitation_id, "en", timezone.now())


def shape(response: Any) -> tuple[int, dict[str, str], bytes]:
    return response.status_code, dict(response.headers), response.content


@pytest.mark.parametrize("kind", UNUSABLE)
def test_every_unusable_token_gets_one_identical_answer(
    workspace: tuple[Tenant, User], kind: str
) -> None:
    tenant, administrator = workspace
    body, previewer = unusable_body(kind, tenant, administrator)
    signed = signed_in(previewer)
    preview({"token": "y" * 43}, signed)
    with CaptureQueriesContext(connection) as unknown_queries:
        reference = preview({"token": "y" * 43})
    with CaptureQueriesContext(connection) as signed_unknown_queries:
        signed_reference = preview({"token": "y" * 43}, signed)

    with CaptureQueriesContext(connection) as queries:
        anonymous = preview(body)
    with CaptureQueriesContext(connection) as signed_queries:
        signed_answer = preview(body, signed)

    assert (anonymous.status_code, anonymous.json()) == (400, UNAVAILABLE)
    assert shape(anonymous) == shape(reference)
    assert shape(signed_answer) == shape(signed_reference)
    assert len(queries) == len(unknown_queries)
    assert len(signed_queries) == len(signed_unknown_queries)


def test_the_acceptor_previews_an_invitation_they_accepted_until_removed(
    workspace: tuple[Tenant, User],
) -> None:
    tenant, administrator = workspace
    invite_workspace_member(administrator, tenant.pk, GUEST)
    token = invitation_token()
    guest = verified_account("guest", GUEST)
    pending = preview({"token": token}, signed_in(guest)).json()
    accept_workspace_invitation(guest, token)

    accepted = preview({"token": token}, signed_in(guest))
    anonymous = preview({"token": token})
    remove_workspace_member(administrator, tenant.pk, guest.pk)
    removed = preview({"token": token}, signed_in(guest))

    assert (accepted.status_code, accepted.json()) == (200, pending)
    assert (anonymous.status_code, anonymous.json()) == (400, UNAVAILABLE)
    assert (removed.status_code, removed.json()) == (400, UNAVAILABLE)
    assert not Membership.objects.filter(tenant=tenant, user=guest).exists()


def test_previews_are_throttled_per_client_address() -> None:
    rates = cast(dict[str, str], settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])
    limited = override_settings(
        REST_FRAMEWORK={
            **settings.REST_FRAMEWORK,
            "DEFAULT_THROTTLE_RATES": {**rates, "invitation": "2/hour"},
        }
    )
    first, second = Client(REMOTE_ADDR="198.51.100.1"), Client(REMOTE_ADDR="198.51.100.2")
    body = {"token": "x" * 43}

    with limited:
        spent = [preview(body, first).status_code for _ in range(2)]
        throttled = preview(body, first)
        other = preview(body, second)

    assert spent == [400, 400]
    assert throttled.status_code == 429
    assert throttled.json()["code"] == "throttled"
    assert throttled["Retry-After"] == str(throttled.json()["retry_after"])
    assert other.status_code == 400
