import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.db import connection, transaction

from pyracast.accounts.invitations import accept_workspace_invitation, invitation_digest
from pyracast.accounts.models import Membership, Tenant, WorkspaceInvitation, WorkspaceRole
from pyracast.accounts.operations import remove_workspace_member
from pyracast.accounts.retention import reclaim_signup
from pyracast.erasure import erase_account
from pyracast.metering.operations import disconnect_epias
from pyracast.plants import training
from pyracast.plants.automation import prepare_due_calibrations, set_calibration_automation
from pyracast.plants.models import CalibrationRun, Plant
from pyracast.plants.preparation import prepare_requested_calibrations
from tests.support.calibration_automation import evaluated_run, import_fit_history
from tests.support.domain import NOW, System
from tests.support.metering import connect, connected
from tests.support.plant_operations import request_run

pytestmark = pytest.mark.django_db(transaction=True)

PERSON = "auth_user"
WORKSPACE = frozenset({"accounts_tenant", "regions_region", "plants_plant"})
ROW_LOCK = re.compile(
    r"\bFOR (?:NO KEY UPDATE|UPDATE|SHARE|KEY SHARE)(?: OF (?P<of>.+?))?(?: NOWAIT| SKIP LOCKED)?$"
)
# An UPDATE or DELETE locks the person rows it writes.
PERSON_WRITE = re.compile(r'^(?:UPDATE|DELETE FROM) "auth_user"\s')
PERSON_ID = re.compile(r'"auth_user"\."id" (?:= %s|IN \((?:%s, )*%s\))')


def locked_tables(sql: str) -> set[str]:
    # Without OF a locking read locks every table of its FROM list, joined ones included.
    match = ROW_LOCK.search(sql.strip())
    if match is None:
        return set()
    if match["of"]:
        return set(re.findall(r'"(\w+)"', match["of"]))
    return set(re.findall(r'\b(?:FROM|JOIN) "(\w+)"', sql[: match.start()]))


def person_ids(sql: str, params: Any) -> frozenset[object] | None:
    """The person ids a statement names by placeholder, or None when it names none."""
    match = PERSON_ID.search(sql)
    if match is None or not params:
        return None
    start = sql[: match.start()].count("%s")
    return frozenset(params[start : start + match[0].count("%s")])


@dataclass
class LockOrder:
    locked: set[str] = field(default_factory=set)
    # The statements that, after their transaction locked a workspace row, lock a person or
    # write a person row the transaction had not locked.
    late: list[str] = field(default_factory=list)


@dataclass
class _Transaction:
    outer: object = None
    workspace: bool = False
    # The person ids locked so far; None once a lock named no ids and so may hold any person.
    persons: frozenset[object] | None = frozenset()

    def holds(self, ids: frozenset[object] | None) -> bool:
        if self.persons is None:
            return True
        return bool(self.persons) if ids is None else ids <= self.persons


@contextmanager
def lock_order() -> Iterator[LockOrder]:
    order = LockOrder()
    state = _Transaction()

    def inspect(execute: Callable[..., Any], sql: str, params: Any, *args: Any) -> Any:
        nonlocal state
        outer = connection.atomic_blocks[0] if connection.atomic_blocks else None
        if outer is None or outer is not state.outer:
            state = _Transaction(outer=outer)
        tables = locked_tables(sql)
        order.locked |= tables
        ids = person_ids(sql, params)
        locks_person = PERSON in tables or (
            PERSON_WRITE.match(sql) is not None and not state.holds(ids)
        )
        if locks_person and state.workspace and outer is not None:
            order.late.append(sql)
        if PERSON in tables:
            state.persons = None if ids is None or state.persons is None else state.persons | ids
        state.workspace = state.workspace or bool(tables & WORKSPACE)
        return execute(sql, params, *args)

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


def reclamation(
    monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant
) -> Callable[[], object]:
    applicant = User.objects.create_user(username="applicant", email="applicant@example.com")
    User.objects.filter(pk=applicant.pk).update(date_joined=NOW - timedelta(days=8))
    EmailAddress.objects.create(user=applicant, email=applicant.email, primary=True)
    tenant = Tenant.objects.create(name="Applicant workspace")
    Membership.objects.create(tenant=tenant, user=applicant, role=WorkspaceRole.ADMIN)

    def reclaim() -> None:
        assert reclaim_signup(applicant.pk, NOW)

    return reclaim


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
        reclamation,
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


@pytest.mark.parametrize("person_locked_first", [False, True])
def test_a_person_row_written_after_a_workspace_lock_is_late_unless_locked_before(
    person_locked_first: bool,
) -> None:
    person = User.objects.create(username="person")
    tenant = Tenant.objects.create(name="Workspace")

    with lock_order() as order, transaction.atomic():
        if person_locked_first:
            list(User.objects.select_for_update().filter(pk=person.pk))
        list(Tenant.objects.select_for_update().filter(pk=tenant.pk))
        User.objects.filter(pk=person.pk).update(first_name="Renamed")

    writes = [sql for sql in order.late if sql.startswith('UPDATE "auth_user"')]
    assert len(writes) == (0 if person_locked_first else 1)
