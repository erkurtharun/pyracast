from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client

from pyracast.accounts.operations import remove_workspace_member
from pyracast.errors import DomainError
from pyracast.notifications.models import RecipientRole
from pyracast.notifications.operations import AddressChoice, configure_reminders
from pyracast.regions.models import AuditEvent
from pyracast.trash import restore_region, trash_region
from tests.support.domain import NOW, System, make_system
from tests.support.notification_groups import member

pytestmark = pytest.mark.django_db(transaction=True)


def url(system: System) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"


def client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def read(system: System) -> dict[str, Any]:
    response = client_for(system.member).get(url(system))
    assert response.status_code == 200, response.content
    return dict(response.json())


def put(system: System, revision: int, **changes: Any) -> Any:
    values = {
        "enabled": True,
        "include_workspace_members": False,
        "language": "en",
        "recipients": ["ops@example.com"],
        "times": ["10:00"],
        "expected_revision": revision,
    } | changes
    return client_for(system.member).put(url(system), values, content_type="application/json")


def test_a_saved_change_advances_the_revision_and_an_unchanged_save_keeps_it() -> None:
    system = make_system(approved=True)
    assert read(system)["revision"] == 0

    first = put(system, 0)
    unchanged = put(system, 1)
    changed = put(system, 1, times=["11:00"])

    assert [answer.status_code for answer in (first, unchanged, changed)] == [200, 200, 200]
    assert [answer.json()["revision"] for answer in (first, unchanged, changed)] == [1, 1, 2]
    assert read(system)["revision"] == 2


def test_a_form_read_before_a_member_removal_cannot_add_the_address_back() -> None:
    system = make_system(approved=True)
    leaving = member(system, "leaving@example.com")
    put(system, 0, recipients=["leaving@example.com", "ops@example.com"])
    stale = read(system)

    remove_workspace_member(system.member, system.tenant.pk, leaving.pk)
    response = put(system, stale["revision"], recipients=["leaving@example.com", "ops@example.com"])

    assert (response.status_code, response.json()["code"]) == (409, "reminder_schedule_changed")
    current = read(system)
    assert current["revision"] == stale["revision"] + 1
    assert current["addresses"]["recipients"] == ["ops@example.com"]


def test_a_second_tab_saving_an_older_revision_is_refused_and_changes_nothing() -> None:
    system = make_system(approved=True)
    put(system, 0)
    put(system, 1, times=["12:00"])

    response = put(system, 1, times=["13:00"], recipients=["other@example.com"])

    assert (response.status_code, response.json()["code"]) == (409, "reminder_schedule_changed")
    current = read(system)
    assert (current["revision"], current["times"]) == (2, ["12:00"])
    assert current["addresses"]["recipients"] == ["ops@example.com"]


def test_a_restore_from_trash_advances_the_revision() -> None:
    system = make_system(approved=True)
    put(system, 0)

    trash_region(system.member, system.tenant.pk, system.region.pk)
    restore_region(system.member, system.tenant.pk, system.region.pk)

    assert read(system)["revision"] == 2
    assert put(system, 1, times=["12:00"]).status_code == 409


def reminder_events(system: System) -> list[tuple[int, int, str, dict[str, int]]]:
    return list(
        AuditEvent.objects.filter(region=system.region, action="reminder_schedule_updated")
        .order_by("pk")
        .values_list("actor_id", "region_id", "action", "details")
    )


def hidden(revision: int) -> dict[str, int]:
    return {"schedule_revision": revision, "visible_recipients": 0, "visible_outside": 0}


def save(system: System, **changes: Any) -> None:
    values: dict[str, Any] = {
        "enabled": True,
        "recipients": ["ops@example.com"],
        "times": ["10:00"],
        "language": "en",
    } | changes
    configure_reminders(system.member, system.tenant.pk, system.region.pk, now=NOW, **values)


def test_each_saved_change_is_audited_once_and_an_unchanged_save_not_at_all() -> None:
    system = make_system(approved=True)

    save(system)
    save(system)
    save(system, recipients=["ops@example.com", "plant@example.com"])

    assert reminder_events(system) == [
        (system.member.pk, system.region.pk, "reminder_schedule_updated", hidden(1)),
        (system.member.pk, system.region.pk, "reminder_schedule_updated", hidden(2)),
    ]


def test_a_visible_save_audits_how_many_addresses_it_shows_and_how_many_are_outside() -> None:
    system = make_system(approved=True)
    save(system, recipients=["ops@example.com", "plant@example.com"])

    save(
        system,
        recipients=["ops@example.com", "plant@example.com"],
        hide_recipients=False,
        address_roles=[AddressChoice("ops@example.com", RecipientRole.TO, frozenset())],
    )

    assert [details for *_, details in reminder_events(system)] == [
        hidden(1),
        {"schedule_revision": 2, "visible_recipients": 1, "visible_outside": 1},
    ]


def test_a_refused_save_leaves_no_audit_event() -> None:
    system = make_system(approved=True)
    save(system)

    with pytest.raises(DomainError) as refusal:
        save(system, recipients=["ops@example.com", "OPS@example.com"])

    assert refusal.value.code == "duplicate_recipients"

    assert len(reminder_events(system)) == 1
