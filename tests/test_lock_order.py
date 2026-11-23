import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client

from pyracast.accounts.invitations import accept_workspace_invitation, invitation_digest
from pyracast.accounts.models import Membership, WorkspaceInvitation
from pyracast.accounts.operations import remove_workspace_member
from pyracast.erasure import erase_account
from pyracast.metering.operations import disconnect_epias
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import RecipientRole, ReminderRecipient, ReminderSchedule
from pyracast.notifications.operations import MemberChoice, configure_reminders
from pyracast.notifications.scheduler import enqueue_due
from pyracast.plants import training
from pyracast.plants.automation import prepare_due_calibrations, set_calibration_automation
from pyracast.plants.models import CalibrationRun, Plant
from pyracast.plants.preparation import prepare_requested_calibrations
from tests.support.calibration_automation import evaluated_run, import_fit_history
from tests.support.domain import NOW, System, make_system
from tests.support.metering import connect, connected
from tests.support.notification_groups import group, member
from tests.support.notification_transmission import replying
from tests.support.notifications import SLOT, chosen, disclosed
from tests.support.plant_operations import request_run

pytestmark = pytest.mark.django_db(transaction=True)

PERSON = "auth_user"
WORKSPACE = frozenset({"accounts_tenant", "regions_region", "plants_plant"})
ROW_LOCK = re.compile(
    r"\bFOR (?:NO KEY UPDATE|UPDATE|SHARE|KEY SHARE)(?: OF (?P<of>.+?))?(?: NOWAIT| SKIP LOCKED)?$"
)


def locked_tables(sql: str) -> set[str]:
    # Without OF a locking read locks every table of its FROM list, joined ones included.
    match = ROW_LOCK.search(sql.strip())
    if match is None:
        return set()
    if match["of"]:
        return set(re.findall(r'"(\w+)"', match["of"]))
    return set(re.findall(r'\b(?:FROM|JOIN) "(\w+)"', sql[: match.start()]))


@dataclass
class LockOrder:
    locked: set[str] = field(default_factory=set)
    # The statements that lock a person after their transaction locked a workspace row.
    late: list[str] = field(default_factory=list)


@contextmanager
def lock_order() -> Iterator[LockOrder]:
    order = LockOrder()
    state: dict[str, Any] = {"outer": None, "workspace": False}

    def inspect(execute: Callable[..., Any], sql: str, *args: Any) -> Any:
        outer = connection.atomic_blocks[0] if connection.atomic_blocks else None
        if outer is None or outer is not state["outer"]:
            state.update(outer=outer, workspace=False)
        tables = locked_tables(sql)
        order.locked |= tables
        if PERSON in tables and state["workspace"] and outer is not None:
            order.late.append(sql)
        state["workspace"] = state["workspace"] or bool(tables & WORKSPACE)
        return execute(sql, *args)

    with connection.execute_wrapper(inspect):
        yield order


def automated(system: System, plant: Plant) -> None:
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )


def exhausted_run(system: System, plant: Plant, attempts: int) -> CalibrationRun:
    automated(system, plant)
    run = request_run(system, plant)
    CalibrationRun.objects.filter(pk=run.pk).update(attempts=attempts)
    return run


def erasure(monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant) -> Callable[[], object]:
    viewer = User.objects.create(username="viewer")
    Membership.objects.create(user=viewer, tenant=system.tenant)
    return lambda: erase_account(viewer.pk, execute=True)


def removal(monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant) -> Callable[[], object]:
    viewer = User.objects.create(username="viewer")
    Membership.objects.create(user=viewer, tenant=system.tenant)
    return lambda: remove_workspace_member(system.member, system.tenant.pk, viewer.pk)


def acceptance(
    monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant
) -> Callable[[], object]:
    guest = User.objects.create(username="guest")
    EmailAddress.objects.create(user=guest, email="guest@example.com", verified=True)
    WorkspaceInvitation.objects.create(
        tenant=system.tenant,
        email="guest@example.com",
        token_hash=invitation_digest("guest-token"),
        created_by=system.member,
        expires_at=NOW + timedelta(days=7),
    )
    return lambda: accept_workspace_invitation(guest, "guest-token")


def source_connection(
    monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant
) -> Callable[[], object]:
    disconnect_epias(plant.pk, actor=system.admin)
    return lambda: connect(plant, system.admin)


def source_disconnection(
    monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant
) -> Callable[[], object]:
    return lambda: disconnect_epias(plant.pk, actor=system.admin)


def due_check(
    monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant
) -> Callable[[], object]:
    automated(system, plant)
    return lambda: prepare_due_calibrations(NOW + timedelta(days=1))


def enabling(monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant) -> Callable[[], object]:
    evaluated_run(system, plant)
    return lambda: automated(system, plant)


def claim(monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant) -> Callable[[], object]:
    run = exhausted_run(system, plant, 3)
    return lambda: training._claim(run.pk, NOW)


def publication(
    monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant
) -> Callable[[], object]:
    automated(system, plant)
    import_fit_history(system, plant)
    run = CalibrationRun.objects.get()
    return lambda: training.run_calibration(run.pk, NOW)


def failure(monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant) -> Callable[[], object]:
    run = exhausted_run(system, plant, 2)

    def unusable(*_args: object) -> object:
        raise ValueError("unusable calibration data")

    monkeypatch.setattr(training, "build_calibration_dataset", unusable)
    return lambda: training.run_calibration(run.pk, NOW)


def requested_preparation(
    monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant
) -> Callable[[], object]:
    automated(system, plant)
    evaluated_run(system, plant)
    Plant.objects.filter(pk=plant.pk).update(calibration_preparation_due_at=NOW)
    return lambda: prepare_requested_calibrations(NOW)


@pytest.mark.parametrize(
    "operation",
    [
        erasure,
        removal,
        acceptance,
        source_connection,
        source_disconnection,
        due_check,
        enabling,
        claim,
        publication,
        failure,
        requested_preparation,
    ],
    ids=lambda operation: operation.__name__,
)
def test_a_transaction_locks_a_person_before_any_workspace_row(
    monkeypatch: pytest.MonkeyPatch,
    operation: Callable[[pytest.MonkeyPatch, System, Plant], Callable[[], object]],
) -> None:
    system, plant, _linked = connected(monkeypatch)
    run = operation(monkeypatch, system, plant)

    with lock_order() as order:
        run()

    assert PERSON in order.locked
    assert order.locked & WORKSPACE
    assert order.late == []


def reminder_save(system: System) -> Callable[[], object]:
    membership = Membership.objects.get(user=system.member).pk
    settings: dict[str, Any] = {
        "enabled": True,
        "recipients": ["ops@example.com"],
        "include_workspace_members": True,
        "times": ["10:00"],
        "language": "en",
        "now": NOW,
        "hide_recipients": False,
        "member_roles": [
            MemberChoice(frozenset({membership}), system.member.email, RecipientRole.CC)
        ],
    }
    digest = disclosed(system.member, system.tenant.pk, system.region.pk, **settings)
    return lambda: configure_reminders(
        system.member, system.tenant.pk, system.region.pk, disclosure=digest, **settings
    )


def reminder_send(system: System) -> Callable[[], object]:
    schedule = group(system, ["ops@example.com"])
    ReminderSchedule.objects.filter(pk=schedule.pk).update(hide_recipients=False)
    ReminderRecipient.objects.filter(schedule=schedule).update(role=RecipientRole.TO)
    chosen(schedule, User.objects.get(username="alice@example.com"), RecipientRole.BCC)
    (delivery_id,) = enqueue_due(SLOT)

    def send() -> object:
        with replying(lambda message: None) as sent:
            assert process_notification(delivery_id, SLOT)
        (message,) = sent
        assert (message.to, message.bcc) == (
            ["ops@example.com", "admin@example.com"],
            ["alice@example.com"],
        )
        return message

    return send


def reminder_read(system: System) -> Callable[[], object]:
    group(system, ["ops@example.com"])
    client = Client()
    client.force_login(system.member)
    return lambda: client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"
    )


def reminder_preview(system: System) -> Callable[[], object]:
    group(system, ["ops@example.com"])
    alice = Membership.objects.get(user__username="alice@example.com").pk
    client = Client()
    client.force_login(system.member)
    body = {
        "enabled": True,
        "include_workspace_members": True,
        "language": "en",
        "recipients": ["ops@example.com"],
        "times": ["10:00"],
        "expected_revision": 1,
        "hide_recipients": False,
        "address_roles": [{"address": "ops@example.com", "role": "to", "memberships": []}],
        "member_roles": [{"memberships": [alice], "address": "alice@example.com", "role": "bcc"}],
    }

    def previewed() -> object:
        response = client.post(
            f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/preview/",
            body,
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        return response

    return previewed


@pytest.mark.parametrize(
    "operation",
    [reminder_save, reminder_send, reminder_read, reminder_preview],
    ids=lambda op: op.__name__,
)
def test_reminders_lock_no_person_after_a_workspace_row(
    operation: Callable[[System], Callable[[], object]],
) -> None:
    system = make_system(approved=True)
    system.member.email = "admin@example.com"
    system.member.save(update_fields=["email"])
    EmailAddress.objects.create(user=system.member, email=system.member.email, verified=True)
    member(system, "alice@example.com")
    run = operation(system)

    with lock_order() as order:
        run()

    assert order.late == []
    assert ("accounts_tenant" in order.locked) == (operation is not reminder_read)
