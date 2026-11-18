import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.db import close_old_connections, connections
from django.test import Client, override_settings
from django.utils import timezone

from pyracast.accounts.audit import logger
from pyracast.accounts.models import Membership, Tenant, WorkspaceInvitation, WorkspaceRole
from pyracast.accounts.operations import reassign_workspace_admin
from tests.support.workspaces import invitation_token

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])

GUEST = "guest@example.com"


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


@pytest.fixture
def security_log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> pytest.LogCaptureFixture:
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    caplog.set_level(logging.INFO, logger="pyracast.security")
    return caplog


def security_lines(log: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        line
        for record in log.records
        if record.name == "pyracast.security"
        and (line := json.loads(record.message))["event"] != "login_succeeded"
    ]


def workspace(name: str) -> tuple[Tenant, User]:
    tenant = Tenant.objects.create(name=name)
    administrator = User.objects.create_user(username=f"{name}-admin", email=f"{name}@example.com")
    Membership.objects.create(tenant=tenant, user=administrator, role=WorkspaceRole.ADMIN)
    return tenant, administrator


def invite(administrator: User, tenant: Tenant, email: str = GUEST) -> Any:
    client = Client(enforce_csrf_checks=True)
    client.force_login(administrator)
    token = client.get("/api/session/").json()["csrf_token"]
    return client.post(
        f"/api/tenants/{tenant.pk}/invitations/",
        {"email": email},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )


def earlier_invitations(
    tenant: Tenant, sender: User, ages: list[timedelta], email: str | None = None
) -> None:
    now = timezone.now()
    rows = WorkspaceInvitation.objects.bulk_create(
        WorkspaceInvitation(
            tenant=tenant,
            created_by=sender,
            email=email or f"earlier-{index}@example.com",
            token_hash=f"{tenant.pk:08x}{index:056x}",
            expires_at=now,
        )
        for index in range(len(ages))
    )
    for row, age in zip(rows, ages, strict=True):
        WorkspaceInvitation.objects.filter(pk=row.pk).update(created_at=now - age)


def assert_wait(response: Any, low: int, high: int) -> None:
    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "invitation_rate_limited"
    assert low <= body["retry_after"] <= high
    assert response["Retry-After"] == str(body["retry_after"])


def test_a_second_workspace_inviting_one_address_is_refused_with_a_real_wait() -> None:
    first, first_admin = workspace("first")
    second, second_admin = workspace("second")
    assert invite(first_admin, first).status_code == 201

    refused = invite(second_admin, second)

    assert_wait(refused, 86_390, 86_400)
    assert refused.json()["message"] == "Too many invitations were sent. Try again later."
    assert not WorkspaceInvitation.objects.filter(tenant=second).exists()


def test_two_workspaces_inviting_one_address_at_once_send_one_invitation() -> None:
    pairs = [workspace("first"), workspace("second")]
    start = Barrier(2)

    def send(pair: tuple[Tenant, User]) -> int:
        close_old_connections()
        try:
            start.wait(timeout=10)
            return int(invite(pair[1], pair[0]).status_code)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(send, pairs))

    assert statuses == [201, 429]
    assert WorkspaceInvitation.objects.filter(email=GUEST).count() == 1


def test_a_resend_waits_out_the_minute_of_the_previous_invitation() -> None:
    tenant, administrator = workspace("solar")
    earlier_invitations(tenant, administrator, [timedelta(seconds=20)], email=GUEST)

    assert_wait(invite(administrator, tenant), 39, 40)


def test_the_hourly_workspace_budget_answers_when_its_oldest_invitation_leaves() -> None:
    tenant, administrator = workspace("solar")
    earlier_invitations(
        tenant, administrator, [timedelta(minutes=50)] + [timedelta(minutes=5)] * 19
    )

    assert_wait(invite(administrator, tenant), 599, 600)


def test_the_daily_sender_budget_spans_the_senders_workspaces() -> None:
    first, sender = workspace("first")
    second = Tenant.objects.create(name="second")
    Membership.objects.create(tenant=second, user=sender, role=WorkspaceRole.ADMIN)
    earlier_invitations(first, sender, [timedelta(hours=23)] + [timedelta(hours=2)] * 24)
    earlier_invitations(second, sender, [timedelta(hours=3)] * 25)

    assert_wait(invite(sender, second), 3_599, 3_600)
    other, other_admin = workspace("other")
    assert invite(other_admin, other).status_code == 201


def test_revoked_invitations_count_towards_the_daily_recipient_budget() -> None:
    tenant, administrator = workspace("solar")
    earlier_invitations(
        tenant,
        administrator,
        [timedelta(hours=20), timedelta(hours=10), timedelta(hours=5)],
        email=GUEST,
    )

    assert_wait(invite(administrator, tenant), 14_399, 14_400)
    assert invite(administrator, tenant, "another@example.com").status_code == 201


def test_an_address_longer_than_the_stored_column_is_a_field_error() -> None:
    tenant, administrator = workspace("solar")
    address = "a" * 64 + "@" + ".".join(["b" * 63, "c" * 63, "d" * 57]) + ".test"
    assert len(address) == 255

    response = invite(administrator, tenant, address)

    assert response.status_code == 400
    assert set(response.json()["fields"]) == {"email"}
    assert not WorkspaceInvitation.objects.exists()


def test_a_refused_invitation_is_recorded_without_the_address(
    security_log: pytest.LogCaptureFixture,
) -> None:
    first, first_admin = workspace("first")
    second, second_admin = workspace("second")
    assert invite(first_admin, first).status_code == 201
    security_log.clear()

    assert invite(second_admin, second).status_code == 429

    [line] = security_lines(security_log)
    assert line["event"] == "invitation_limited"
    assert (line["user_id"], line["tenant_id"], line["reason"]) == (
        second_admin.pk,
        second.pk,
        "recipient_workspace",
    )
    assert len(line["subject"]) == 16
    assert GUEST not in security_log.text


def test_invitation_and_membership_changes_are_recorded_without_addresses(
    security_log: pytest.LogCaptureFixture,
) -> None:
    tenant, administrator = workspace("solar")
    sent = invite(administrator, tenant)
    assert sent.status_code == 201
    token = invitation_token()
    guest = User.objects.create_user(username="guest", email=GUEST)
    EmailAddress.objects.create(user=guest, email=GUEST, primary=True, verified=True)
    member = Client(enforce_csrf_checks=True)
    member.force_login(guest)
    csrf = member.get("/api/session/").json()["csrf_token"]
    accepted = member.post(
        "/api/invitations/accept/",
        {"token": token},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert accepted.status_code == 200
    revoked = invite(administrator, tenant, "later@example.com").json()["id"]
    admin = Client(enforce_csrf_checks=True)
    admin.force_login(administrator)
    csrf = admin.get("/api/session/").json()["csrf_token"]
    path = f"/api/tenants/{tenant.pk}"
    assert admin.delete(f"{path}/invitations/{revoked}/", HTTP_X_CSRFTOKEN=csrf).status_code == 204
    assert admin.delete(f"{path}/members/{guest.pk}/", HTTP_X_CSRFTOKEN=csrf).status_code == 204
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=tenant, user=viewer)
    operator = User.objects.create_superuser(username="operator", email="operator@example.com")
    reassign_workspace_admin(operator, tenant.pk, viewer.pk, expected_admin_id=administrator.pk)

    lines = [
        (line["event"], line["user_id"], line["tenant_id"], line.get("target_id"))
        for line in security_lines(security_log)
    ]
    assert lines == [
        ("invitation_sent", administrator.pk, tenant.pk, sent.json()["id"]),
        ("invitation_accepted", guest.pk, tenant.pk, sent.json()["id"]),
        ("invitation_sent", administrator.pk, tenant.pk, revoked),
        ("invitation_revoked", administrator.pk, tenant.pk, revoked),
        ("workspace_member_removed", administrator.pk, tenant.pk, guest.pk),
        ("workspace_admin_reassigned", operator.pk, tenant.pk, viewer.pk),
    ]
    assert GUEST not in security_log.text
    assert "later@example.com" not in security_log.text
