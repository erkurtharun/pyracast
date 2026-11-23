from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.db.models import Model
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    NotificationSettings,
    ReminderMemberRole,
    ReminderRecipient,
    ReminderRecipientTie,
    ReminderSchedule,
)
from pyracast.notifications.scheduler import enqueue_due
from pyracast.regions.models import AuditEvent
from tests.support.domain import NOW, System, make_system
from tests.support.notification_groups import member
from tests.support.notification_transmission import replying
from tests.support.notifications import SLOT

pytestmark = pytest.mark.django_db(transaction=True)

ADMIN = "admin@example.com"
STORED: tuple[type[Model], ...] = (
    NotificationSettings,
    ReminderSchedule,
    ReminderRecipient,
    ReminderRecipientTie,
    ReminderMemberRole,
    AuditEvent,
    NotificationDelivery,
    DeliveryRecipient,
)


@pytest.fixture(autouse=True)
def saved_at_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)


def workspace() -> tuple[System, User, User]:
    system = make_system(approved=True)
    system.member.email = ADMIN
    system.member.save(update_fields=["email"])
    EmailAddress.objects.create(user=system.member, email=ADMIN, verified=True, primary=True)
    return system, member(system, "alice@example.com"), member(system, "bob@example.com")


def membership(user: User) -> int:
    return Membership.objects.get(user=user).pk


def stored() -> dict[str, list[dict[str, Any]]]:
    return {
        model.__name__: list(model._default_manager.order_by("pk").values()) for model in STORED
    }


def url(system: System, suffix: str = "") -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/{suffix}"


def client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def body(system: System, alice: User, bob: User, **changes: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "include_workspace_members": True,
        "language": "en",
        "recipients": ["boss@example.com", "outside@example.com"],
        "times": ["10:00"],
        "expected_revision": 0,
        "hide_recipients": False,
        "address_roles": [{"address": "boss@example.com", "role": "to", "memberships": []}],
        "member_roles": [
            {"memberships": [membership(alice)], "address": alice.email, "role": "bcc"},
            {"memberships": [membership(bob)], "address": bob.email, "role": "cc"},
        ],
    } | changes


def preview(system: System, payload: dict[str, Any]) -> Any:
    return client_for(system.member).post(
        url(system, "preview/"), payload, content_type="application/json"
    )


def save(system: System, payload: dict[str, Any]) -> Any:
    return client_for(system.member).put(url(system), payload, content_type="application/json")


def with_a_sent_delivery(system: System, alice: User, bob: User) -> dict[str, Any]:
    assert save(system, body(system, alice, bob)).status_code == 200
    (delivery_id,) = enqueue_due(SLOT)
    with replying(lambda message: None):
        assert process_notification(delivery_id, SLOT)
    assert DeliveryRecipient.objects.exclude(role="").exists()
    return body(
        system,
        alice,
        bob,
        expected_revision=1,
        recipients=["outside@example.com", "new@example.com"],
        address_roles=[{"address": "outside@example.com", "role": "cc", "memberships": []}],
        member_roles=[
            {"memberships": [membership(alice)], "address": alice.email, "role": None},
            {"memberships": [membership(bob)], "address": bob.email, "role": "to"},
        ],
    )


def without_a_schedule(system: System, alice: User, bob: User) -> dict[str, Any]:
    return body(system, alice, bob)


@pytest.mark.parametrize("arrange", [with_a_sent_delivery, without_a_schedule])
def test_a_preview_answers_what_the_save_then_answers_and_writes_nothing(arrange: Any) -> None:
    system, alice, bob = workspace()
    payload = arrange(system, alice, bob)
    before = stored()

    previewed = preview(system, payload)

    assert previewed.status_code == 200, previewed.content
    assert stored() == before
    assert previewed.json()["revision"] == payload["expected_revision"] + 1
    saved = save(system, payload)
    assert (saved.status_code, saved.json()) == (200, previewed.json())
    assert stored() != before


def stale_revision(system: System, alice: User, bob: User) -> dict[str, Any]:
    assert save(system, body(system, alice, bob)).status_code == 200
    return body(system, alice, bob, recipients=["changed@example.com"], address_roles=[])


def stale_member_entry(system: System, alice: User, bob: User) -> dict[str, Any]:
    entry = {"memberships": [membership(alice)], "address": bob.email, "role": None}
    return body(system, alice, bob, member_roles=[entry])


def duplicate_recipients(system: System, alice: User, bob: User) -> dict[str, Any]:
    return body(
        system,
        alice,
        bob,
        recipients=["OPS@XN--BCHER-KVA.DE", "ops@xn--bcher-kva.de"],
        address_roles=[],
    )


@pytest.mark.parametrize(
    ("arrange", "refusal"),
    [
        (stale_revision, (409, "reminder_schedule_changed")),
        (stale_member_entry, (409, "reminder_schedule_changed")),
        (duplicate_recipients, (400, "duplicate_recipients")),
    ],
)
def test_a_refused_preview_answers_the_saves_refusal_and_writes_nothing(
    arrange: Any, refusal: tuple[int, str]
) -> None:
    system, alice, bob = workspace()
    payload = arrange(system, alice, bob)
    before = stored()

    previewed = preview(system, payload)

    assert (previewed.status_code, previewed.json()["code"]) == refusal
    assert stored() == before
    saved = save(system, payload)
    assert (saved.status_code, saved.json()) == (previewed.status_code, previewed.json())
    assert stored() == before


def test_a_viewer_is_refused_a_preview_and_nothing_is_written() -> None:
    system, alice, bob = workspace()
    Membership.objects.filter(user=alice).update(role=WorkspaceRole.VIEWER)
    before = stored()

    response = client_for(alice).post(
        url(system, "preview/"), body(system, alice, bob), content_type="application/json"
    )

    assert (response.status_code, response.json()["code"]) == (403, "workspace_admin_required")
    assert stored() == before
