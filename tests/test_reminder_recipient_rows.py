import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.db import close_old_connections, connection, connections, transaction
from django.test import override_settings

from pyracast.accounts.operations import remove_workspace_member
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.scheduler import enqueue_due
from tests.support.concurrency import behind_open_transaction
from tests.support.domain import NOW, System
from tests.support.notification_groups import group, member, second_workspace
from tests.support.notification_transmission import BACKEND, OpeningBackend
from tests.support.notifications import SLOT, configured, reminder_schedule_payload

pytestmark = pytest.mark.django_db(transaction=True)


def explicit(system: System, addresses: list[str], *, members: bool = False) -> None:
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=addresses,
        include_workspace_members=members,
        times=["10:00"],
        language="en",
        now=NOW,
    )


def test_the_history_of_a_waiting_delivery_leaves_out_an_address_removed_with_its_member() -> None:
    system, _ = configured()
    leaving = member(system, "leaving@example.com")
    member(system, "staying@example.com")
    schedule = group(system)
    enqueue_due(SLOT)

    remove_workspace_member(system.member, system.tenant.pk, leaving.pk)

    administrator, viewer = (
        reminder_schedule_payload(schedule, SLOT, administrator=flag)["deliveries"][0]
        for flag in (True, False)
    )
    assert administrator["recipients"] == ["staying@example.com"]
    assert (administrator["recipient_count"], viewer["recipient_count"]) == (1, 1)


def test_a_workspace_member_is_mailed_at_the_stored_form_of_the_address() -> None:
    system, _ = configured()
    member(system, "Mixed.Case@Example.com")
    schedule = group(system)
    (delivery_id,) = enqueue_due(SLOT)

    assert process_notification(delivery_id, SLOT)

    assert mail.outbox[0].bcc == ["Mixed.Case@Example.com"]
    addresses = reminder_schedule_payload(schedule, SLOT)["addresses"]
    assert addresses["workspace_recipients"] == ["Mixed.Case@Example.com"]


def test_an_address_written_in_capitals_leaves_with_the_member_holding_it_in_lower_case() -> None:
    system, _ = configured()
    holder = member(system, "ops@strasse.de")
    schedule = group(system, ["OPS@Strasse.de", "kept@example.com"])

    remove_workspace_member(system.member, system.tenant.pk, holder.pk)

    schedule.refresh_from_db()
    assert reminder_schedule_payload(schedule, SLOT)["addresses"]["recipients"] == [
        "kept@example.com"
    ]


def test_removing_a_member_leaves_the_same_address_in_another_workspace() -> None:
    system, _ = configured()
    holder = member(system, "shared@example.com")
    explicit(system, ["shared@example.com"])
    other = second_workspace(system)
    configure_reminders(
        other.member,
        other.tenant.pk,
        other.region.pk,
        enabled=True,
        recipients=["shared@example.com"],
        times=["10:00"],
        language="en",
        now=NOW,
    )
    delivery_ids = enqueue_due(SLOT)

    remove_workspace_member(system.member, system.tenant.pk, holder.pk)

    assert [process_notification(key, SLOT) for key in delivery_ids] == [False, True]
    assert [message.bcc for message in mail.outbox] == [["shared@example.com"]]


def test_an_address_whose_removal_commits_while_the_claim_waits_is_not_mailed() -> None:
    system, _ = configured()
    leaving = member(system, "leaving@example.com")
    member(system, "staying@example.com")
    group(system)
    (delivery_id,) = enqueue_due(SLOT)

    _, delivered = behind_open_transaction(
        lambda: remove_workspace_member(system.member, system.tenant.pk, leaving.pk),
        lambda: process_notification(delivery_id, SLOT),
    )

    assert delivered
    assert [message.bcc for message in mail.outbox] == [["staying@example.com"]]


def remove(system: System, user: User) -> Callable[[], object]:
    return lambda: remove_workspace_member(system.member, system.tenant.pk, user.pk)


def drop_external(system: System) -> Callable[[], None]:
    return lambda: explicit(system, ["kept@example.com"])


def _backend_pid() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def _blocked(pid: int) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [pid])
        row = cursor.fetchone()
    return bool(row and row[0])


@pytest.fixture
def opening_backend() -> Iterator[None]:
    OpeningBackend.action, OpeningBackend.reply, OpeningBackend.sent = None, None, []
    try:
        with override_settings(MAILERS={"default": {"BACKEND": BACKEND}}):
            yield
    finally:
        OpeningBackend.action = None


def transmit_while(change: Callable[[], object], delivery_id: int) -> bool:
    """Commits `change` only once the transmit marker of the delivery waits for its locks."""
    held, commit = Event(), Event()
    pids: list[int] = []
    changes: list[Future[None]] = []

    def hold() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                change()
                held.set()
                assert commit.wait(10)
        finally:
            connections.close_all()

    def deliver() -> bool:
        close_old_connections()
        try:
            pids.append(_backend_pid())
            return process_notification(delivery_id, SLOT)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(2) as pool:

        def start_change() -> None:
            changes.append(pool.submit(hold))
            assert held.wait(10)

        OpeningBackend.action = start_change
        delivering = pool.submit(deliver)
        try:
            deadline = time.monotonic() + 10
            while not delivering.done() and not (held.is_set() and _blocked(pids[0])):
                assert time.monotonic() < deadline, "The marker did not wait for the change."
                commit.wait(0.02)
        finally:
            commit.set()
        for future in changes:
            future.result(timeout=20)
        return delivering.result(timeout=20)


@pytest.mark.usefixtures("opening_backend")
def test_an_address_whose_removal_commits_while_the_marker_waits_is_not_mailed() -> None:
    system, _ = configured()
    leaving = member(system, "leaving@example.com")
    member(system, "staying@example.com")
    group(system)
    (delivery_id,) = enqueue_due(SLOT)

    assert transmit_while(remove(system, leaving), delivery_id)

    assert [message.bcc for message in OpeningBackend.sent] == [["staying@example.com"]]


@pytest.mark.usefixtures("opening_backend")
def test_an_address_dropped_by_a_save_while_the_marker_waits_is_not_mailed() -> None:
    system, _ = configured()
    explicit(system, ["external@example.com", "kept@example.com"])
    (delivery_id,) = enqueue_due(SLOT)

    assert transmit_while(drop_external(system), delivery_id)

    assert [message.bcc for message in OpeningBackend.sent] == [["kept@example.com"]]


@pytest.mark.usefixtures("opening_backend")
def test_an_address_left_out_at_the_claim_does_not_return_at_the_marker() -> None:
    system, _ = configured()
    paused = member(system, "paused@example.com")
    member(system, "staying@example.com")
    group(system)
    (delivery_id,) = enqueue_due(SLOT)
    User.objects.filter(pk=paused.pk).update(is_active=False)

    def reactivate() -> None:
        User.objects.filter(pk=paused.pk).update(is_active=True)

    OpeningBackend.action = reactivate
    assert process_notification(delivery_id, SLOT)

    assert [message.bcc for message in OpeningBackend.sent] == [["staying@example.com"]]
