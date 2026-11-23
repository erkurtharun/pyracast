import json
from datetime import timedelta
from io import StringIO
from itertools import count
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from pyracast import erasure
from pyracast.accounts.invitations import accept_workspace_invitation, invitation_digest
from pyracast.accounts.models import Membership, Tenant, WorkspaceInvitation, WorkspaceRole
from pyracast.accounts.operations import reassign_workspace_admin, remove_workspace_member
from pyracast.erasure import erase_account
from pyracast.errors import ConflictError
from pyracast.mail.errors import NotSentError
from pyracast.notifications import operations, views
from pyracast.notifications import recipients as recipient_rows
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    NotificationSettings,
    RecipientRole,
    ReminderMemberRole,
    ReminderRecipient,
    ReminderSchedule,
)
from pyracast.notifications.operations import (
    MAX_RECIPIENT_LIMIT,
    AddressChoice,
    MemberChoice,
    configure_reminders,
)
from pyracast.notifications.retention import trim_rendered_mail
from pyracast.notifications.scheduler import enqueue_due
from pyracast.regions.models import AuditEvent
from tests.support.concurrency import committed_elsewhere, paused_in
from tests.support.domain import NOW, System, make_system
from tests.support.notification_groups import member
from tests.support.notification_transmission import OpeningBackend, replying
from tests.support.notifications import (
    SLOT,
    StatementHook,
    after_statement,
    chosen,
    moved,
    pending_address_change,
    ties,
)

pytestmark = pytest.mark.django_db(transaction=True)

TO, CC, BCC = RecipientRole.TO, RecipientRole.CC, RecipientRole.BCC
ADMIN = "admin@example.com"


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


def url(system: System) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"


def client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def revision(system: System) -> int:
    return (
        ReminderSchedule.objects.filter(region=system.region)
        .values_list("revision", flat=True)
        .first()
        or 0
    )


def put(system: System, **changes: Any) -> Any:
    body = {
        "enabled": True,
        "include_workspace_members": True,
        "language": "en",
        "recipients": ["boss@example.com", "outside@example.com"],
        "times": ["10:00"],
        "expected_revision": revision(system),
    } | changes
    return client_for(system.member).put(url(system), body, content_type="application/json")


def stored_roles(system: System) -> tuple[dict[str, str], dict[str, str]]:
    schedule = ReminderSchedule.objects.get(region=system.region)
    return (
        dict(ReminderRecipient.objects.filter(schedule=schedule).values_list("key", "role")),
        {
            row.membership.user.email: row.role
            for row in ReminderMemberRole.objects.filter(schedule=schedule).select_related(
                "membership__user"
            )
        },
    )


def events(system: System) -> list[dict[str, int]]:
    return list(
        AuditEvent.objects.filter(region=system.region, action="reminder_schedule_updated")
        .order_by("pk")
        .values_list("details", flat=True)
    )


def to_for(address: str, *members: User) -> list[dict[str, Any]]:
    return [{"address": address, "role": "to", "memberships": [membership(m) for m in members]}]


def visible_save(system: System, alice: User, bob: User, **changes: Any) -> Any:
    return put(
        system,
        **{
            "hide_recipients": False,
            "address_roles": [{"address": "boss@example.com", "role": "to", "memberships": []}],
            "member_roles": [
                {"membership": membership(alice), "address": alice.email, "role": "bcc"},
                {"membership": membership(bob), "address": bob.email, "role": "cc"},
            ],
        }
        | changes,
    )


def test_a_plant_without_a_schedule_answers_the_visible_default() -> None:
    system, _, _ = workspace()

    body = client_for(system.member).get(url(system)).json()

    assert body["hide_recipients"] is False
    assert [entry["kind"] for entry in body["addresses"]["members"]] == [
        "administrator",
        "member",
        "member",
    ]


def test_a_save_with_roles_stores_them_and_answers_them_to_the_administrator() -> None:
    system, alice, bob = workspace()

    response = visible_save(system, alice, bob)

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["hide_recipients"] is False
    assert body["addresses"]["members"] == [
        {
            "membership": membership(system.member),
            "address": ADMIN,
            "kind": "administrator",
            "role": None,
        },
        {"membership": membership(alice), "address": alice.email, "kind": "member", "role": "bcc"},
        {"membership": membership(bob), "address": bob.email, "kind": "member", "role": "cc"},
    ]
    assert body["addresses"]["added"] == [
        {"address": "boss@example.com", "role": "to", "eligible": True, "memberships": []},
        {"address": "outside@example.com", "role": None, "eligible": True, "memberships": []},
    ]
    assert stored_roles(system) == (
        {"boss@example.com": "to", "outside@example.com": ""},
        {alice.email: "bcc", bob.email: "cc"},
    )
    assert events(system) == [
        {"schedule_revision": 1, "visible_recipients": 3, "visible_outside": 1}
    ]


def test_a_save_without_the_added_members_saves_hidden_and_keeps_every_choice() -> None:
    system, alice, bob = workspace()
    visible_save(system, alice, bob)

    response = put(system, recipients=["boss@example.com", "new@example.com"])

    assert response.status_code == 200, response.content
    assert response.json()["hide_recipients"] is True
    assert stored_roles(system) == (
        {"boss@example.com": "to", "new@example.com": ""},
        {alice.email: "bcc", bob.email: "cc"},
    )
    assert events(system)[-1] == {
        "schedule_revision": 2,
        "visible_recipients": 0,
        "visible_outside": 0,
    }


def test_a_save_replaces_typed_choices_and_the_named_members_and_preserves_the_others() -> None:
    system, alice, bob = workspace()
    visible_save(system, alice, bob)

    response = put(
        system,
        hide_recipients=False,
        address_roles=[{"address": "OUTSIDE@example.com", "role": "cc", "memberships": []}],
        member_roles=[{"membership": membership(alice), "address": alice.email, "role": None}],
    )

    assert response.status_code == 200, response.content
    assert stored_roles(system) == (
        {"boss@example.com": "", "outside@example.com": "cc"},
        {bob.email: "cc"},
    )


def test_an_unchanged_save_writes_nothing() -> None:
    system, alice, bob = workspace()
    visible_save(system, alice, bob)

    response = visible_save(system, alice, bob)

    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert len(events(system)) == 1


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (
            {"address_roles": [{"address": "nobody@example.com", "role": "to", "memberships": []}]},
            "address_roles",
        ),
        (
            {
                "address_roles": [
                    {"address": "boss@example.com", "role": "to", "memberships": []},
                    {"address": "Boss@example.com", "role": "cc", "memberships": []},
                ]
            },
            "address_roles",
        ),
        (
            {"address_roles": [{"address": "boss@example.com", "role": "all", "memberships": []}]},
            "address_roles",
        ),
        (
            {"address_roles": [{"address": "boss@example.com", "role": "to", "memberships": [0]}]},
            "address_roles",
        ),
        (
            {"member_roles": [{"membership": 1, "address": ADMIN, "role": "to", "extra": 1}]},
            "member_roles",
        ),
    ],
)
def test_malformed_role_entries_are_refused_on_their_field(
    change: dict[str, Any], field: str
) -> None:
    system, _, _ = workspace()

    response = put(system, **change)

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert [name.split(".")[0] for name in response.json()["fields"]] == [field]
    assert not ReminderSchedule.objects.exists()


def test_two_entries_for_one_member_are_refused() -> None:
    system, alice, _ = workspace()
    entry = {"membership": membership(alice), "address": alice.email, "role": "cc"}

    response = put(system, member_roles=[entry, entry | {"role": "to"}])

    assert response.status_code == 400
    assert list(response.json()["fields"]) == ["member_roles"]


def other_workspace_membership() -> int:
    outsider = User.objects.create(username="outsider", email="outsider@example.com")
    EmailAddress.objects.create(user=outsider, email=outsider.email, verified=True, primary=True)
    other = Tenant.objects.create(name="Other")
    return Membership.objects.create(tenant=other, user=outsider).pk


@pytest.mark.parametrize(
    "case",
    [
        "outdated address",
        "inactive member",
        "unverified primary",
        "removed member",
        "other workspace",
    ],
)
def test_a_member_entry_that_no_longer_matches_refuses_the_whole_save(case: str) -> None:
    system, alice, bob = workspace()
    visible_save(system, alice, bob)
    entry = {"membership": membership(bob), "address": bob.email, "role": "to"}
    if case == "outdated address":
        EmailAddress.objects.filter(user=bob).update(email="bob.new@example.com")
        User.objects.filter(pk=bob.pk).update(email="bob.new@example.com")
    elif case == "inactive member":
        User.objects.filter(pk=bob.pk).update(is_active=False)
    elif case == "unverified primary":
        EmailAddress.objects.filter(user=bob).update(verified=False)
    elif case == "removed member":
        remove_workspace_member(system.member, system.tenant.pk, bob.pk)
    else:
        entry = {
            "membership": other_workspace_membership(),
            "address": "outsider@example.com",
            "role": "to",
        }
    before = (revision(system), stored_roles(system), len(events(system)))

    response = put(
        system,
        hide_recipients=False,
        recipients=["changed@example.com"],
        member_roles=[entry],
    )

    assert response.status_code == 409
    assert response.json()["code"] == "reminder_schedule_changed"
    assert "@" not in json.dumps(response.json())
    assert (revision(system), stored_roles(system), len(events(system))) == before


def test_a_stale_revision_is_refused_as_before() -> None:
    system, alice, bob = workspace()
    visible_save(system, alice, bob)

    response = put(system, expected_revision=0, hide_recipients=False)

    assert response.status_code == 409
    assert response.json()["code"] == "reminder_schedule_changed"


def test_a_visible_schedule_over_the_limit_is_paused_by_an_earlier_release_request() -> None:
    system, alice, bob = workspace()
    visible_save(system, alice, bob)
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 2})

    response = put(system, enabled=False)

    assert response.status_code == 200, response.content
    assert (response.json()["enabled"], response.json()["hide_recipients"]) == (False, True)


def test_a_viewer_reads_the_mode_and_no_address_membership_kind_or_role() -> None:
    system, alice, bob = workspace()
    Membership.objects.filter(user=alice).update(role=WorkspaceRole.VIEWER)
    visible_save(system, alice, bob)
    with replying(lambda message: None):
        (delivery_id,) = enqueue_due(SLOT)
        assert process_notification(delivery_id, SLOT)

    body = client_for(alice).get(url(system)).json()

    assert body["hide_recipients"] is False
    assert body["addresses"] is None
    assert [delivery["recipient_roles"] for delivery in body["deliveries"]] == [None]
    text = json.dumps(body)
    assert "@" not in text
    assert all(word not in text for word in ('"membership"', '"kind"', '"role"'))


def test_the_history_answers_each_address_with_its_header_after_the_marker() -> None:
    system, alice, bob = workspace()
    visible_save(system, alice, bob)
    (delivery_id,) = enqueue_due(SLOT)

    before = client_for(system.member).get(url(system)).json()["deliveries"]
    with replying(lambda message: None):
        assert process_notification(delivery_id, SLOT)
    after = client_for(system.member).get(url(system)).json()["deliveries"]

    assert [delivery["recipient_roles"] for delivery in before] == [None]
    (delivery,) = after
    assert delivery["recipient_roles"] == [
        {"address": address, "role": role}
        for address, role in zip(
            delivery["recipients"],
            ["to", "bcc", "to", "bcc", "cc"],
            strict=True,
        )
    ]


def test_a_typed_to_whose_tied_member_gave_the_address_up_is_answered_without_a_role() -> None:
    system, alice, _ = workspace()
    put(
        system,
        include_workspace_members=False,
        recipients=[alice.email],
        hide_recipients=False,
        address_roles=to_for(alice.email, alice),
    )
    moved(alice, "alice.new@example.com")

    body = client_for(system.member).get(url(system)).json()

    assert body["addresses"]["added"] == [
        {"address": "alice@example.com", "role": None, "eligible": True, "memberships": []}
    ]


def test_a_reassignment_waits_for_a_save_and_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, alice, bob = workspace()

    def save() -> tuple[int, list[str]]:
        response = visible_save(system, alice, bob)
        return response.status_code, [
            row["kind"] for row in response.json()["addresses"]["members"]
        ]

    def reassign() -> bool:
        return reassign_workspace_admin(
            system.admin, system.tenant.pk, alice.pk, expected_admin_id=system.member.pk
        )

    (status, kinds), reassigned = paused_in(monkeypatch, views, "reminder_overview", save, reassign)

    assert status == 200
    assert kinds == ["administrator", "member", "member"]
    assert reassigned
    assert Membership.objects.get(user=alice).role == WorkspaceRole.ADMIN


def save_naming(system: System, user: User) -> str:
    try:
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            enabled=True,
            recipients=[],
            include_workspace_members=True,
            times=["10:00"],
            language="en",
            now=NOW,
            hide_recipients=False,
            member_roles=[MemberChoice(membership(user), user.email, BCC)],
        )
    except ConflictError as error:
        return error.code
    return "saved"


def remove(system: System, user: User) -> str:
    return "removed" if remove_workspace_member(system.member, system.tenant.pk, user.pk) else ""


def erase(_: System, user: User) -> str:
    return "erased" if erase_account(user.pk, execute=True).changes else ""


@pytest.mark.parametrize(
    ("deletion", "owner", "step"),
    [
        (remove, recipient_rows, "remove_member_recipients"),
        (erase, erasure, "_forget_identity"),
    ],
)
@pytest.mark.parametrize("save_first", [True, False])
def test_a_save_racing_a_member_deletion_ends_without_the_members_choice(
    monkeypatch: pytest.MonkeyPatch,
    deletion: Any,
    owner: object,
    step: str,
    save_first: bool,
) -> None:
    system, alice, bob = workspace()
    chosen(
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            enabled=True,
            recipients=[],
            include_workspace_members=True,
            times=["10:00"],
            language="en",
            now=NOW,
        ),
        bob,
        CC,
    )

    def saving() -> str:
        return save_naming(system, alice)

    def deleting() -> str:
        return str(deletion(system, alice))

    if save_first:
        saved, deleted = paused_in(
            monkeypatch, operations, "store_member_choices", saving, deleting
        )
        assert saved == "saved"
    else:
        deleted, saved = paused_in(monkeypatch, owner, step, deleting, saving)
        assert saved == "reminder_schedule_changed"

    assert deleted in {"removed", "erased"}
    assert not Membership.objects.filter(user=alice).exists()
    assert list(ReminderMemberRole.objects.values_list("membership__user__username", "role")) == [
        ("bob@example.com", "cc")
    ]


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_an_address_confirmed_after_a_saves_read_of_people_leaves_the_save_one_picture() -> None:
    system, _, bob = workspace()
    confirm = pending_address_change(bob, "bob.new@example.com")
    hook = StatementHook("account_emailaddress", lambda: committed_elsewhere(confirm))

    with after_statement(hook):
        schedule = configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            enabled=True,
            recipients=["bob@example.com"],
            include_workspace_members=True,
            times=["10:00"],
            language="en",
            now=NOW,
            hide_recipients=False,
            address_roles=[AddressChoice("bob@example.com", TO, frozenset({membership(bob)}))],
            member_roles=[MemberChoice(membership(bob), "bob@example.com", CC)],
        )

    assert hook.fired
    assert User.objects.get(pk=bob.pk).email == "bob.new@example.com"
    assert ties(schedule) == {"bob@example.com": [membership(bob)]}
    assert list(ReminderRecipient.objects.values_list("role", "role_members")) == [
        ("to", [membership(bob)])
    ]
    assert stored_roles(system)[1] == {"bob.new@example.com": "cc"}
    assert events(system) == [
        {"schedule_revision": 1, "visible_recipients": 3, "visible_outside": 0}
    ]


def test_a_get_answers_the_state_its_membership_check_read() -> None:
    system, alice, _ = workspace()
    put(system)

    def reassign_and_save() -> None:
        committed_elsewhere(
            lambda: reassign_workspace_admin(
                system.admin, system.tenant.pk, alice.pk, expected_admin_id=system.member.pk
            )
        )
        committed_elsewhere(
            lambda: configure_reminders(
                alice,
                system.tenant.pk,
                system.region.pk,
                enabled=True,
                recipients=["boss@example.com", "unknown@example.com"],
                include_workspace_members=True,
                times=["10:00"],
                language="en",
                now=NOW,
            )
        )

    # The route's permission reads the membership first; the view checks it again.
    hook = StatementHook("accounts_membership", reassign_and_save, skip=1)

    with after_statement(hook):
        body = client_for(system.member).get(url(system)).json()

    assert hook.fired
    assert "unknown@example.com" not in json.dumps(body)
    assert body["addresses"]["recipients"] == ["boss@example.com", "outside@example.com"]
    assert client_for(system.member).get(url(system)).json()["addresses"] is None


def invited(system: System, number: int) -> tuple[User, str]:
    guest = User.objects.create(username=f"guest{number}", email=f"guest{number}@example.com")
    EmailAddress.objects.create(user=guest, email=guest.email, verified=True, primary=True)
    token = f"guest-token-{number}"
    WorkspaceInvitation.objects.create(
        tenant=system.tenant,
        email=guest.email,
        token_hash=invitation_digest(token),
        created_by=system.member,
        expires_at=NOW + timedelta(days=7),
    )
    return guest, token


def joining(guest: User, token: str) -> Any:
    return lambda: committed_elsewhere(lambda: accept_workspace_invitation(guest, token))


def test_a_member_who_joins_during_a_get_is_wholly_in_its_answer_or_wholly_absent() -> None:
    system, alice, _ = workspace()
    put(system, recipients=[alice.email, "outside@example.com"])
    client = Client(raise_request_exception=False)
    client.force_login(system.member)

    for passed in count():
        guest, token = invited(system, passed)
        hook = StatementHook("accounts_membership", joining(guest, token), skip=passed)
        with after_statement(hook):
            response = client.get(url(system))
        if not hook.fired:
            break
        assert response.status_code == 200, (passed, response.content)
        body = response.json()
        listed = body["addresses"]["workspace_recipients"]
        assert (guest.email in listed) == (
            guest.email in [row["address"] for row in body["addresses"]["members"]]
        )
        assert body["workspace_member_count"] == len(listed)

    assert passed >= 2


FOLDED = ("ops@strasse.de", "ops@straße.de")


def folded_members() -> tuple[System, User, User]:
    system, _, _ = workspace()
    return system, member(system, FOLDED[0]), member(system, FOLDED[1])


@pytest.mark.parametrize("named", [0, 1])
def test_an_entry_naming_either_member_of_a_folded_address_stores_the_choice_for_both(
    named: int,
) -> None:
    system, lower, higher = folded_members()
    choosing = (lower, higher)[named]

    response = put(
        system,
        recipients=["boss@example.com"],
        hide_recipients=False,
        member_roles=[{"membership": membership(choosing), "address": FOLDED[named], "role": "cc"}],
    )

    assert response.status_code == 200, response.content
    assert stored_roles(system)[1] == {FOLDED[0]: "cc", FOLDED[1]: "cc"}
    (row,) = [row for row in response.json()["addresses"]["members"] if row["address"] == FOLDED[0]]
    assert row == {
        "membership": membership(lower),
        "address": FOLDED[0],
        "kind": "member",
        "role": "cc",
    }


def test_the_answer_combines_the_choices_and_kinds_of_a_folded_address() -> None:
    system, lower, higher = folded_members()
    put(system, recipients=["boss@example.com"])
    schedule = ReminderSchedule.objects.get(region=system.region)
    chosen(schedule, lower, CC)
    chosen(schedule, higher, TO)
    reassign_workspace_admin(
        system.admin, system.tenant.pk, higher.pk, expected_admin_id=system.member.pk
    )

    rows = client_for(higher).get(url(system)).json()["addresses"]["members"]

    assert [row for row in rows if row["address"] == FOLDED[0]] == [
        {
            "membership": membership(lower),
            "address": FOLDED[0],
            "kind": "administrator",
            "role": "bcc",
        }
    ]


def test_two_entries_for_one_folded_address_are_refused() -> None:
    system, lower, higher = folded_members()

    response = put(
        system,
        recipients=["boss@example.com"],
        member_roles=[
            {"membership": membership(lower), "address": FOLDED[0], "role": "cc"},
            {"membership": membership(higher), "address": FOLDED[1], "role": "to"},
        ],
    )

    assert response.status_code == 400
    assert list(response.json()["fields"]) == ["member_roles"]
    assert not ReminderSchedule.objects.exists()


def sent_headers(day: int) -> tuple[list[str], list[str], list[str]]:
    slot = SLOT + timedelta(days=day)
    with replying(lambda message: None) as sent:
        delivery_id = enqueue_due(slot)[-1]
        assert process_notification(delivery_id, slot)
    (message,) = sent
    return list(message.to), list(message.cc), list(message.bcc)


def test_a_typed_to_whose_address_another_member_took_is_hidden_until_chosen_for_them() -> None:
    system, alice, bob = workspace()
    address = alice.email
    body = {"include_workspace_members": False, "recipients": [address], "hide_recipients": False}
    assert put(system, **body, address_roles=to_for(address, alice)).status_code == 200
    assert sent_headers(0) == ([address], [], [])
    moved(alice, "alice.new@example.com")
    moved(bob, address)
    unchanged = (revision(system), len(events(system)))

    assert sent_headers(1) == ([], [], [address])

    assert put(system, **body).status_code == 200
    assert (revision(system), len(events(system))) == unchanged
    assert sent_headers(2) == ([], [], [address])

    repeated = put(system, **body, address_roles=to_for(address, alice))
    assert repeated.json()["addresses"]["added"] == [
        {"address": address, "role": None, "eligible": True, "memberships": [membership(bob)]}
    ]
    assert sent_headers(3) == ([], [], [address])
    dropped = (revision(system), len(events(system)))

    chosen_again = put(system, **body, address_roles=to_for(address, bob))
    assert chosen_again.json()["addresses"]["added"] == [
        {"address": address, "role": "to", "eligible": True, "memberships": [membership(bob)]}
    ]
    assert (revision(system), len(events(system))) == (dropped[0] + 1, dropped[1] + 1)
    assert events(system)[-1]["visible_recipients"] == 1
    assert sent_headers(4) == ([address], [], [])


def test_a_typed_to_chosen_for_the_new_holder_alone_is_a_change() -> None:
    system, alice, bob = workspace()
    address = alice.email
    body = {"include_workspace_members": False, "recipients": [address], "hide_recipients": False}
    put(system, **body, address_roles=to_for(address, alice))
    moved(alice, "alice.new@example.com")
    moved(bob, address)
    before = (revision(system), len(events(system)))

    put(system, **body, address_roles=to_for(address, bob))

    assert (revision(system), len(events(system))) == (before[0] + 1, before[1] + 1)
    assert list(ReminderRecipient.objects.values_list("role", "role_members")) == [
        ("to", [membership(bob)])
    ]


def test_a_to_chosen_again_for_an_address_nobody_holds_is_sent_to() -> None:
    system, alice, _ = workspace()
    address = alice.email
    body = {"include_workspace_members": False, "recipients": [address], "hide_recipients": False}
    put(system, **body, address_roles=to_for(address, alice))
    moved(alice, "alice.new@example.com")
    assert sent_headers(0) == ([], [], [address])

    put(system, **body, address_roles=to_for(address))

    assert sent_headers(1) == ([address], [], [])


def test_an_outside_to_that_a_member_makes_primary_rests_until_chosen_again() -> None:
    system, _, bob = workspace()
    outside = "outside@example.com"
    put(system, recipients=[outside], hide_recipients=False, address_roles=to_for(outside))
    assert sent_headers(0) == ([outside, ADMIN], ["alice@example.com", "bob@example.com"], [])

    moved(bob, outside)

    assert sent_headers(1) == ([ADMIN], [outside, "alice@example.com"], [])


def test_a_typed_to_whose_tied_members_are_all_inactive_is_not_sent_and_has_no_role() -> None:
    system, alice, _ = workspace()
    put(
        system,
        include_workspace_members=False,
        recipients=[alice.email, "boss@example.com"],
        hide_recipients=False,
        address_roles=to_for(alice.email, alice),
    )
    User.objects.filter(pk=alice.pk).update(is_active=False)

    assert sent_headers(0) == ([], [], ["boss@example.com"])
    added = client_for(system.member).get(url(system)).json()["addresses"]["added"]
    assert added[0] == {
        "address": "alice@example.com",
        "role": None,
        "eligible": False,
        "memberships": [],
    }


def test_a_changed_role_alone_advances_the_revision_and_writes_the_event() -> None:
    system, alice, bob = workspace()
    visible_save(system, alice, bob)
    before = (revision(system), len(events(system)))

    visible_save(
        system,
        alice,
        bob,
        address_roles=[{"address": "boss@example.com", "role": "cc", "memberships": []}],
    )

    assert (revision(system), len(events(system))) == (before[0] + 1, before[1] + 1)


def history_roles(system: System) -> list[Any]:
    deliveries = client_for(system.member).get(url(system)).json()["deliveries"]
    return [(delivery["status"], delivery["recipient_roles"]) for delivery in deliveries]


def rejected(message: Any) -> None:
    raise NotSentError("rejected")


def test_a_rejected_attempt_answers_no_roles_before_or_after_its_retry_is_claimed() -> None:
    system, alice, bob = workspace()
    visible_save(system, alice, bob)
    (delivery_id,) = enqueue_due(SLOT)
    with replying(rejected):
        assert not process_notification(delivery_id, SLOT)
    assert DeliveryRecipient.objects.filter(delivery_id=delivery_id, role=TO).exists()

    assert history_roles(system) == [("retry", None)]

    seen: list[Any] = []

    def refused_at_opening() -> None:
        seen.append(history_roles(system))
        raise OSError("connection refused")

    try:
        with replying(lambda message: None):
            OpeningBackend.action = refused_at_opening
            while (delivery := NotificationDelivery.objects.get(pk=delivery_id)).status == "retry":
                process_notification(delivery_id, delivery.available_at)
    finally:
        OpeningBackend.action = None

    assert seen
    assert all(roles == [("sending", None)] for roles in seen)
    assert history_roles(system) == [("failed", None)]


def test_a_retry_that_is_sent_answers_its_own_roles() -> None:
    system, alice, bob = workspace()
    visible_save(system, alice, bob)
    (delivery_id,) = enqueue_due(SLOT)
    with replying(rejected):
        assert not process_notification(delivery_id, SLOT)
    ReminderRecipient.objects.filter(key="boss@example.com").update(role=BCC)

    with replying(lambda message: None):
        delivery = NotificationDelivery.objects.get(pk=delivery_id)
        assert process_notification(delivery_id, delivery.available_at)

    ((status, roles),) = history_roles(system)
    assert status == "sent"
    assert {"address": "boss@example.com", "role": "bcc"} in roles
    assert [entry["role"] for entry in roles].count("to") == 1


def test_a_visible_send_interrupted_after_its_marker_answers_its_roles_and_is_not_sent_again() -> (
    None
):
    system, alice, bob = workspace()
    visible_save(system, alice, bob)
    (delivery_id,) = enqueue_due(SLOT)

    transmitted: list[Any] = []

    def interrupted(message: Any) -> None:
        transmitted.append(message)
        raise RuntimeError("connection lost after DATA")

    with replying(interrupted):
        assert not process_notification(delivery_id, SLOT)
        assert not process_notification(delivery_id, SLOT + timedelta(minutes=5))

    assert len(transmitted) == 1
    ((status, roles),) = history_roles(system)
    assert status == "uncertain"
    assert roles[0] == {"address": "boss@example.com", "role": "to"}


def test_the_published_role_lists_carry_their_bounds() -> None:
    output = StringIO()
    call_command("spectacular", format="openapi-json", stdout=output)
    schemas = json.loads(output.getvalue())["components"]["schemas"]
    lists = schemas["ReminderInput"]["properties"]
    entry = schemas["AddressRoleInput"]["properties"]["memberships"]

    assert [lists[name].get("maxItems") for name in ("address_roles", "member_roles")] == [
        MAX_RECIPIENT_LIMIT,
        MAX_RECIPIENT_LIMIT,
    ]
    assert (entry.get("maxItems"), entry["items"].get("minimum")) == (MAX_RECIPIENT_LIMIT, 1)
    assert schemas["MemberRoleInput"]["properties"]["membership"].get("minimum") == 1


def test_a_delivery_row_erasure_keeps_for_another_verified_holder_keeps_its_visible_role() -> None:
    system, _, _ = workspace()
    lower, _ = member(system, FOLDED[0]), member(system, FOLDED[1])
    put(system, recipients=["boss@example.com"], hide_recipients=False)
    assert FOLDED[0] in sent_headers(0)[1]

    assert erase_account(lower.pk, execute=True).changes

    assert list(DeliveryRecipient.objects.filter(key=FOLDED[0]).values_list("role", flat=True)) == [
        CC
    ]


def test_a_delivery_row_the_retention_sweep_passes_keeps_its_visible_role() -> None:
    system, alice, bob = workspace()
    visible_save(system, alice, bob)
    sent_headers(0)
    rows = list(DeliveryRecipient.objects.order_by("pk").values_list("address", "role"))

    assert trim_rendered_mail(SLOT + timedelta(days=1), 10) == 1

    assert ("boss@example.com", TO) in rows
    assert list(DeliveryRecipient.objects.order_by("pk").values_list("address", "role")) == rows
    ((status, roles),) = history_roles(system)
    assert (status, [(entry["address"], entry["role"]) for entry in roles]) == ("sent", rows)
