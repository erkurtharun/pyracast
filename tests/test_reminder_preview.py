from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.db.models import Model
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.accounts.operations import reassign_workspace_admin
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
from tests.support.concurrency import behind_open_transaction
from tests.support.domain import NOW, System, make_system
from tests.support.notification_groups import member
from tests.support.notification_transmission import replying
from tests.support.notifications import SLOT, confirmed_put, pending_address_change

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
    return confirmed_put(client_for(system.member), url(system), payload)


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
    answer = previewed.json()
    saved = save(system, payload | {"disclosure": answer.pop("disclosure")})
    assert (saved.status_code, saved.json()) == (200, answer)
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


def members_only(system: System, **changes: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "include_workspace_members": True,
        "language": "en",
        "recipients": [],
        "times": ["10:00"],
        "expected_revision": revision(system),
        "hide_recipients": False,
    } | changes


def revision(system: System) -> int:
    schedule = ReminderSchedule.objects.filter(region=system.region).first()
    return schedule.revision if schedule else 0


def disclosure(system: System, payload: dict[str, Any]) -> str:
    answered = preview(system, payload)
    assert answered.status_code == 200, answered.content
    digest: str = answered.json()["disclosure"]
    return digest


def put_as(user: User, system: System, payload: dict[str, Any]) -> Any:
    return client_for(user).put(url(system), payload, content_type="application/json")


def test_a_visible_save_with_its_previews_disclosure_is_stored_and_recorded() -> None:
    system, _, _ = workspace()
    payload = members_only(system)
    digest = disclosure(system, payload)

    saved = put_as(system.member, system, payload | {"disclosure": digest})

    assert saved.status_code == 200, saved.content
    assert "disclosure" not in saved.json()
    schedule = ReminderSchedule.objects.get(region=system.region)
    assert (schedule.hide_recipients, schedule.revision) == (False, 1)
    assert AuditEvent.objects.filter(action="reminder_schedule_updated").count() == 1


def changed_holder(system: System, alice: User, bob: User) -> User:
    pending_address_change(bob, "bob.new@example.com")()
    return system.member


def joined_member(system: System, alice: User, bob: User) -> User:
    member(system, "carol@example.com")
    return system.member


def reassigned(system: System, alice: User, bob: User) -> User:
    assert reassign_workspace_admin(
        system.admin, system.tenant.pk, alice.pk, expected_admin_id=system.member.pk
    )
    return alice


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
@pytest.mark.parametrize("change", [changed_holder, joined_member, reassigned])
def test_a_change_of_people_after_the_preview_refuses_the_visible_save(change: Any) -> None:
    system, alice, bob = workspace()
    assert (
        confirmed_put(client_for(system.member), url(system), members_only(system)).status_code
        == 200
    )
    payload = members_only(system)
    digest = disclosure(system, payload)
    saver = change(system, alice, bob)
    before = stored()

    refused = put_as(saver, system, payload | {"disclosure": digest})

    assert (refused.status_code, refused.json()["code"]) == (409, "reminder_disclosure_changed")
    assert stored() == before


@pytest.mark.parametrize("given", [{}, {"disclosure": "0" * 64}])
def test_a_visible_save_without_its_previews_disclosure_is_refused(given: dict[str, str]) -> None:
    system, _, _ = workspace()
    before = stored()

    refused = put_as(system.member, system, members_only(system) | given)

    assert (refused.status_code, refused.json()["code"]) == (409, "reminder_disclosure_changed")
    assert stored() == before


@pytest.mark.parametrize("given", [{}, {"disclosure": "0" * 64}])
def test_a_hidden_save_needs_no_disclosure(given: dict[str, str]) -> None:
    system, _, _ = workspace()

    saved = put_as(system.member, system, members_only(system, hide_recipients=True) | given)

    assert saved.status_code == 200, saved.content
    assert ReminderSchedule.objects.get(region=system.region).revision == 1


def test_a_malformed_disclosure_is_refused_at_its_field() -> None:
    system, _, _ = workspace()

    refused = put_as(system.member, system, members_only(system, disclosure="A" * 64))

    assert refused.status_code == 400, refused.content
    assert list(refused.json()["fields"]) == ["disclosure"]


def test_the_disclosure_follows_the_visible_lists_and_their_order() -> None:
    system, _, _ = workspace()
    to: list[dict[str, Any]] = [
        {"address": address, "role": "to", "memberships": []} for address in ("x@a.org", "y@a.org")
    ]

    first = members_only(system, include_workspace_members=False, recipients=["x@a.org", "y@a.org"])
    swapped = first | {"recipients": ["y@a.org", "x@a.org"]}
    digests = [
        disclosure(system, first | {"address_roles": to}),
        disclosure(system, first | {"address_roles": to}),
        disclosure(system, swapped | {"address_roles": to}),
        disclosure(system, first),
    ]

    assert digests[0] == digests[1]
    assert len({digests[0], digests[2], digests[3]}) == 3
    assert all(len(digest) == 64 and int(digest, 16) >= 0 for digest in digests)


def test_a_hidden_preview_answers_a_disclosure_and_the_get_and_a_viewer_answer_none() -> None:
    system, alice, _ = workspace()
    payload = members_only(system, hide_recipients=True)

    previewed = preview(system, payload)
    assert confirmed_put(client_for(system.member), url(system), payload).status_code == 200
    Membership.objects.filter(user=alice).update(role=WorkspaceRole.VIEWER)
    answers = [client_for(user).get(url(system)).json() for user in (system.member, alice)]

    assert len(previewed.json()["disclosure"]) == 64
    assert ["disclosure" in answer for answer in answers] == [False, False]
    assert answers[1]["addresses"] is None


def test_a_change_committed_while_the_save_waits_for_its_lock_refuses_it() -> None:
    system, _, _ = workspace()
    payload = members_only(system)
    digest = disclosure(system, payload)

    def join() -> None:
        Tenant.objects.select_for_update().get(pk=system.tenant.pk)
        member(system, "carol@example.com")

    def save_previewed() -> tuple[int, str]:
        response = put_as(system.member, system, payload | {"disclosure": digest})
        return response.status_code, response.json().get("code", "")

    _, refused = behind_open_transaction(join, save_previewed)

    assert refused == (409, "reminder_disclosure_changed")
    assert not ReminderSchedule.objects.exists()
