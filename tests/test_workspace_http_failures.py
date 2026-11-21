from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, cast

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, connections, transaction
from django.test import Client, override_settings

from pyracast.accounts.invitations import accept_workspace_invitation, invite_workspace_member
from pyracast.accounts.models import Membership, Tenant, WorkspaceInvitation, WorkspaceRole
from pyracast.errors import MESSAGES, ErrorCode
from tests.support.workspaces import (
    ACCEPT,
    PREVIEW,
    administered_workspace,
    invitation_token,
    post,
    signed_in,
    verified_account,
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])

CONTRACT_KEYS = {"code", "message", "fields", "retry_after", "request_id"}


@dataclass(frozen=True)
class Workspace:
    tenant: Tenant
    administrator: User
    viewer: User
    outsider: User

    @property
    def base(self) -> str:
        return f"/api/tenants/{self.tenant.pk}/"


@pytest.fixture
def workspace() -> Workspace:
    tenant, administrator = administered_workspace()
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=tenant, user=viewer, role=WorkspaceRole.VIEWER)
    outsider = User.objects.create_user(username="outsider", email="outsider@example.com")
    return Workspace(tenant, administrator, viewer, outsider)


def held_workspace(space: Workspace, request: Callable[[], Any]) -> Any:
    """Answers `request` while another transaction holds the workspace row."""

    def contended() -> Any:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '100ms'")
            return request()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool, transaction.atomic():
        Tenant.objects.select_for_update().get(pk=space.tenant.pk)
        return pool.submit(contended).result(timeout=15)


def accepted_invitation(space: Workspace) -> WorkspaceInvitation:
    invite_workspace_member(space.administrator, space.tenant.pk, "joined@example.com")
    accept_workspace_invitation(
        verified_account("joined", "joined@example.com"), invitation_token()
    )
    return WorkspaceInvitation.objects.get(email="joined@example.com")


def one_preview_an_hour() -> override_settings:
    rates = cast(dict[str, str], settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])
    return override_settings(
        REST_FRAMEWORK={
            **settings.REST_FRAMEWORK,
            "DEFAULT_THROTTLE_RATES": {**rates, "invitation": "1/hour"},
        }
    )


def throttled_preview() -> Any:
    client = Client()
    with one_preview_an_hour():
        post(client, PREVIEW, {"token": "x" * 43})
        return post(client, PREVIEW, {"token": "x" * 43})


def repeated_invitation(space: Workspace) -> Any:
    client = signed_in(space.administrator)
    post(client, f"{space.base}invitations/", {"email": "guest@example.com"})
    return post(client, f"{space.base}invitations/", {"email": "guest@example.com"})


def immediate_resend(space: Workspace) -> Any:
    invitation = invite_workspace_member(space.administrator, space.tenant.pk, "guest@example.com")
    return signed_in(space.administrator).post(f"{space.base}invitations/{invitation.pk}/resend/")


def unverified_acceptance(space: Workspace) -> Any:
    invite_workspace_member(space.administrator, space.tenant.pk, "guest@example.com")
    return post(signed_in(space.outsider), ACCEPT, {"token": invitation_token()})


FAILURES: dict[str, tuple[Callable[[Workspace], Any], int, ErrorCode]] = {
    "members_anonymous": (
        lambda space: Client().get(f"{space.base}members/"),
        401,
        ErrorCode.NOT_AUTHENTICATED,
    ),
    "members_outsider": (
        lambda space: signed_in(space.outsider).get(f"{space.base}members/"),
        403,
        ErrorCode.MEMBERSHIP_REQUIRED,
    ),
    "members_viewer": (
        lambda space: signed_in(space.viewer).get(f"{space.base}members/"),
        403,
        ErrorCode.WORKSPACE_ADMIN_REQUIRED,
    ),
    "members_out_of_range_workspace": (
        lambda space: signed_in(space.administrator).get(
            "/api/tenants/9223372036854775808/members/"
        ),
        403,
        ErrorCode.MEMBERSHIP_REQUIRED,
    ),
    "remove_the_administrator": (
        lambda space: signed_in(space.administrator).delete(
            f"{space.base}members/{space.administrator.pk}/"
        ),
        400,
        ErrorCode.WORKSPACE_ADMIN_NOT_REMOVABLE,
    ),
    "remove_without_csrf": (
        lambda space: signed_in(space.administrator, enforce_csrf_checks=True).delete(
            f"{space.base}members/{space.viewer.pk}/"
        ),
        403,
        ErrorCode.CSRF_FAILED,
    ),
    "remove_as_viewer": (
        lambda space: signed_in(space.viewer).delete(
            f"{space.base}members/{space.administrator.pk}/"
        ),
        403,
        ErrorCode.WORKSPACE_ADMIN_REQUIRED,
    ),
    "remove_while_the_workspace_is_held": (
        lambda space: held_workspace(
            space,
            lambda: signed_in(space.administrator).delete(
                f"{space.base}members/{space.viewer.pk}/"
            ),
        ),
        503,
        ErrorCode.BUSY,
    ),
    "invite_an_invalid_address": (
        lambda space: post(
            signed_in(space.administrator), f"{space.base}invitations/", {"email": "nobody"}
        ),
        400,
        ErrorCode.INVALID_REQUEST,
    ),
    "invite_with_an_unknown_field": (
        lambda space: post(
            signed_in(space.administrator),
            f"{space.base}invitations/",
            {"email": "guest@example.com", "role": "admin"},
        ),
        400,
        ErrorCode.INVALID_REQUEST,
    ),
    "invite_a_member": (
        lambda space: post(
            signed_in(space.administrator),
            f"{space.base}invitations/",
            {"email": space.viewer.email},
        ),
        400,
        ErrorCode.INVITATION_MEMBER_EXISTS,
    ),
    "invite_again_at_once": (repeated_invitation, 429, ErrorCode.INVITATION_RATE_LIMITED),
    "invite_as_viewer": (
        lambda space: post(
            signed_in(space.viewer), f"{space.base}invitations/", {"email": "guest@example.com"}
        ),
        403,
        ErrorCode.WORKSPACE_ADMIN_REQUIRED,
    ),
    "invite_while_the_workspace_is_held": (
        lambda space: held_workspace(
            space,
            lambda: post(
                signed_in(space.administrator),
                f"{space.base}invitations/",
                {"email": "guest@example.com"},
            ),
        ),
        503,
        ErrorCode.BUSY,
    ),
    "cancel_an_unknown_invitation": (
        lambda space: signed_in(space.administrator).delete(f"{space.base}invitations/999999/"),
        400,
        ErrorCode.INVITATION_UNAVAILABLE,
    ),
    "cancel_an_accepted_invitation": (
        lambda space: signed_in(space.administrator).delete(
            f"{space.base}invitations/{accepted_invitation(space).pk}/"
        ),
        400,
        ErrorCode.INVITATION_UNAVAILABLE,
    ),
    "cancel_as_outsider": (
        lambda space: signed_in(space.outsider).delete(f"{space.base}invitations/1/"),
        403,
        ErrorCode.MEMBERSHIP_REQUIRED,
    ),
    "resend_an_accepted_invitation": (
        lambda space: signed_in(space.administrator).post(
            f"{space.base}invitations/{accepted_invitation(space).pk}/resend/"
        ),
        400,
        ErrorCode.INVITATION_UNAVAILABLE,
    ),
    "resend_at_once": (immediate_resend, 429, ErrorCode.INVITATION_RATE_LIMITED),
    "resend_as_viewer": (
        lambda space: signed_in(space.viewer).post(f"{space.base}invitations/1/resend/"),
        403,
        ErrorCode.WORKSPACE_ADMIN_REQUIRED,
    ),
    "accept_anonymous": (
        lambda space: post(Client(), ACCEPT, {"token": "x" * 43}),
        401,
        ErrorCode.NOT_AUTHENTICATED,
    ),
    "accept_an_unknown_token": (
        lambda space: post(signed_in(space.outsider), ACCEPT, {"token": "x" * 43}),
        400,
        ErrorCode.INVITATION_UNAVAILABLE,
    ),
    "accept_with_another_address": (
        unverified_acceptance,
        403,
        ErrorCode.INVITATION_EMAIL_UNVERIFIED,
    ),
    "accept_an_over_long_token": (
        lambda space: post(signed_in(space.outsider), ACCEPT, {"token": "x" * 129}),
        400,
        ErrorCode.INVALID_REQUEST,
    ),
    "preview_an_unknown_token": (
        lambda space: post(Client(), PREVIEW, {"token": "x" * 43}),
        400,
        ErrorCode.INVITATION_UNAVAILABLE,
    ),
    "preview_too_often": (lambda space: throttled_preview(), 429, ErrorCode.THROTTLED),
    "preview_with_a_get": (lambda space: Client().get(PREVIEW), 405, ErrorCode.METHOD_NOT_ALLOWED),
}


@pytest.mark.parametrize("failure", FAILURES)
def test_every_workspace_failure_answers_through_the_shared_error_contract(
    workspace: Workspace, failure: str
) -> None:
    request, status, code = FAILURES[failure]

    response = request(workspace)

    assert response.status_code == status
    assert response["Content-Type"] == "application/json"
    body = response.json()
    assert body["code"] == code
    assert set(body) <= CONTRACT_KEYS
    if code == ErrorCode.INVALID_REQUEST:
        assert isinstance(body["message"], str)
        assert body["message"]
    else:
        assert body["message"] == MESSAGES[code]
    if status in {429, 503}:
        assert response["Retry-After"] == str(body["retry_after"])
    else:
        assert "Retry-After" not in response
