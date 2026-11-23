from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.db import IntegrityError, connection
from django.test import Client
from django.utils import timezone

from pyracast import erasure
from pyracast.accounts.models import Membership
from pyracast.accounts.operations import remove_workspace_member
from pyracast.erasure import erase_account
from pyracast.notifications import operations
from pyracast.notifications import recipients as recipient_rows
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import RecipientRole, ReminderSchedule
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.scheduler import enqueue_due
from pyracast.regions.models import AuditEvent
from tests.support.concurrency import committed_elsewhere, paused_in
from tests.support.domain import NOW, System, make_system
from tests.support.notification_groups import member
from tests.support.notification_transmission import replying
from tests.support.notifications import SLOT, chosen, moved, pending_address_change

pytestmark = pytest.mark.django_db(transaction=True)

ADMIN = "admin@example.com"
SHARED = "shared@example.com"
UNICODE, ACE = "ops@b\u00fccher.de", "ops@xn--bcher-kva.de"


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


def url(system: System, suffix: str = "") -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/{suffix}"


def schedule_of(system: System) -> ReminderSchedule:
    return ReminderSchedule.objects.get(region=system.region)


def answer(system: System, user: User) -> dict[str, Any]:
    client = Client()
    client.force_login(user)
    body: dict[str, Any] = client.get(url(system)).json()
    return body


def save(system: System, suffix: str = "", **changes: Any) -> Any:
    client = Client()
    client.force_login(system.member)
    body = {
        "enabled": True,
        "include_workspace_members": True,
        "language": "en",
        "recipients": [],
        "times": ["10:00"],
        "expected_revision": schedule_of(system).revision
        if ReminderSchedule.objects.filter(region=system.region).exists()
        else 0,
        "hide_recipients": False,
    } | changes
    send = client.post if suffix else client.put
    response = send(url(system, suffix), body, content_type="application/json")
    assert response.status_code == 200, response.content
    return response.json()


def events(system: System) -> int:
    return AuditEvent.objects.filter(
        region=system.region, action="reminder_schedule_updated"
    ).count()


def remove(system: System, user: User) -> bool:
    return remove_workspace_member(system.member, system.tenant.pk, user.pk)


def erase(_: System, user: User) -> bool:
    return bool(erase_account(user.pk, execute=True).changes)


DEPARTURES = [pytest.param(remove, id="removal"), pytest.param(erase, id="erasure")]


def headers_of(delivery_id: int, slot: Any) -> tuple[list[str], list[str], list[str]]:
    with replying(lambda message: None) as sent:
        assert process_notification(delivery_id, slot)
    (message,) = sent
    return list(message.to), list(message.cc), sorted(message.bcc)


def next_send(day: int) -> tuple[list[str], list[str], list[str]]:
    slot = SLOT + timedelta(days=day)
    return headers_of(enqueue_due(slot)[-1], slot)


def hidden_typed_row_of(system: System, alice: User, typed: str) -> None:
    moved(alice, typed)
    save(
        system,
        recipients=[typed],
        address_roles=[{"address": typed, "role": "bcc", "memberships": [membership(alice)]}],
    )
    moved(alice, "alice.new@example.com")


@contextmanager
def after_sql(prefix: str, action: Callable[[], object]) -> Iterator[list[bool]]:
    fired: list[bool] = []

    def run(execute: Callable[..., Any], sql: str, *args: Any) -> Any:
        result = execute(sql, *args)
        if not fired and sql.startswith(prefix):
            fired.append(True)
            action()
        return result

    with connection.execute_wrapper(run):
        yield fired


HIDING = 'UPDATE "notifications_reminderschedule" SET "hide_recipients"'
DELETION = 'DELETE FROM "notifications_reminderrecipient"'


TYPED = {"other spelling": UNICODE}


def successor(case: str, bob: User) -> Callable[[Callable[[], bool]], bool]:
    """Bob takes the address over before the departure or while it runs."""
    if case == "same spelling":
        moved(bob, SHARED)
        return lambda depart: depart()
    if case == "other spelling":
        moved(bob, ACE)
        return lambda depart: depart()
    confirm = pending_address_change(bob, SHARED)

    def during(depart: Callable[[], bool]) -> bool:
        with after_sql(HIDING if case == "after the hiding" else DELETION, confirm_once) as fired:
            departed = depart()
        assert fired
        return departed

    def confirm_once() -> None:
        committed_elsewhere(confirm)

    return during


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
@pytest.mark.parametrize("departure", DEPARTURES)
@pytest.mark.parametrize(
    "case", ["same spelling", "other spelling", "after the hiding", "after the deletion"]
)
def test_a_departure_hides_the_visible_schedule_a_successor_of_the_address_would_see(
    departure: Callable[[System, User], bool], case: str
) -> None:
    system, alice, bob = workspace()
    hidden_typed_row_of(system, alice, TYPED.get(case, SHARED))
    around = successor(case, bob)
    (queued,) = enqueue_due(SLOT)
    before = schedule_of(system)
    logged = events(system)

    assert around(lambda: departure(system, alice))

    sends = [headers_of(queued, SLOT), next_send(1)]
    after = schedule_of(system)
    assert ([(to, cc) for to, cc, _ in sends], after.hide_recipients) == ([([], [])] * 2, True)
    assert after.hidden_by_departure
    assert (after.configured_by_id, after.effective_from) == (
        before.configured_by_id,
        before.effective_from,
    )
    assert events(system) == logged
    if departure is remove:
        assert after.revision == before.revision + 1
    assert User.objects.get(pk=bob.pk).email in (SHARED, ACE)
    assert answer(system, system.member)["hidden_by_departure"] is True
    assert answer(system, bob)["hidden_by_departure"] is None


@pytest.mark.parametrize("departure", DEPARTURES)
def test_a_departure_hides_a_resting_typed_to_whose_empty_binding_would_match_again(
    departure: Callable[[System, User], bool],
) -> None:
    system, _, _ = workspace()
    save(
        system,
        recipients=[ACE],
        address_roles=[{"address": ACE, "role": "to", "memberships": []}],
    )
    carol = member(system, "carol@example.com")
    chosen(schedule_of(system), carol, RecipientRole.BCC)
    moved(carol, UNICODE)
    assert next_send(0)[0] == [ADMIN]
    queued = enqueue_due(SLOT + timedelta(days=1))[-1]
    before = schedule_of(system)

    assert departure(system, carol)

    sends = [headers_of(queued, SLOT + timedelta(days=1)), next_send(2)]
    assert [(to, cc, ACE in bcc) for to, cc, bcc in sends] == [([], [], True)] * 2
    after = schedule_of(system)
    assert (after.hide_recipients, after.hidden_by_departure, after.revision) == (
        True,
        True,
        before.revision + 1,
    )

    previewed = save(system, "preview/", recipients=[ACE])
    saved = save(system, recipients=[ACE])

    assert previewed["addresses"] == saved["addresses"]
    assert [(row["address"], row["placement"]) for row in saved["addresses"]["added"]] == [
        (ACE, "to")
    ]
    assert (saved["hidden_by_departure"], saved["revision"]) == (False, after.revision + 1)
    assert events(system) == 2
    assert next_send(3) == ([ACE, ADMIN], ["alice@example.com", "bob@example.com"], [])


@pytest.mark.parametrize("departure", DEPARTURES)
def test_a_departure_leaves_a_hidden_schedule_as_it_is(
    departure: Callable[[System, User], bool],
) -> None:
    system, alice, _ = workspace()
    save(system, hide_recipients=True)
    before = schedule_of(system)

    assert departure(system, alice)

    after = schedule_of(system)
    assert (after.hide_recipients, after.hidden_by_departure, after.revision) == (
        True,
        False,
        before.revision,
    )


@pytest.mark.parametrize("departure", DEPARTURES)
def test_the_next_save_clears_the_mark_and_is_recorded(
    departure: Callable[[System, User], bool],
) -> None:
    system, alice, _ = workspace()
    save(system)
    assert departure(system, alice)
    hidden = schedule_of(system)
    logged = events(system)

    saved = save(system, hide_recipients=True)

    assert (saved["hide_recipients"], saved["hidden_by_departure"]) == (True, False)
    assert saved["revision"] == hidden.revision + 1
    assert events(system) == logged + 1


def test_the_database_refuses_a_departure_mark_on_a_visible_schedule() -> None:
    system, _, _ = workspace()
    save(system)

    with pytest.raises(IntegrityError):
        ReminderSchedule.objects.filter(region=system.region).update(hidden_by_departure=True)


def saving(system: System) -> Callable[[], str]:
    def run() -> str:
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
        )
        return "saved"

    return run


@pytest.mark.parametrize(
    ("departure", "owner", "step"),
    [
        pytest.param(remove, recipient_rows, "remove_member_recipients", id="removal"),
        pytest.param(erase, erasure, "_forget_identity", id="erasure"),
    ],
)
@pytest.mark.parametrize("save_first", [True, False])
def test_a_save_racing_a_departure_ends_with_the_mode_the_later_one_leaves(
    monkeypatch: pytest.MonkeyPatch,
    departure: Callable[[System, User], bool],
    owner: object,
    step: str,
    save_first: bool,
) -> None:
    system, alice, _ = workspace()
    save(system, hide_recipients=True)

    def departing() -> str:
        return "departed" if departure(system, alice) else ""

    if save_first:
        saved, departed = paused_in(
            monkeypatch, operations, "store_member_choices", saving(system), departing
        )
    else:
        departed, saved = paused_in(monkeypatch, owner, step, departing, saving(system))

    assert (saved, departed) == ("saved", "departed")
    after = schedule_of(system)
    assert (after.hide_recipients, after.hidden_by_departure) == (save_first, save_first)
