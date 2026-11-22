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
PARENTHESIZED = re.compile(r"\([^()]*\)")
# An UPDATE or DELETE locks the person rows it writes.
PERSON_WRITE = re.compile(r'^(?:UPDATE|DELETE FROM) "auth_user"\s')
# A filter on the person table alone, without a join or subquery; ids prove a target only as a
# top-level conjunct of it.
PERSON_FILTER = re.compile(
    r'^(?:UPDATE "auth_user" SET .+|DELETE FROM "auth_user"|SELECT .+ FROM "auth_user")'
    r" WHERE (?P<where>.+?)(?: ORDER BY .+?)?(?: LIMIT \d+)?(?: FOR .+)?$"
)
PERSON_KEY = re.compile(
    r'(?:^\(?|\bAND )(?P<key>"auth_user"\."id" (?:= %s|IN \((?:%s, )*%s\)))(?=\)?$| AND )'
)


def locked_tables(sql: str) -> set[str]:
    # Without OF a locking read locks every table of its outer FROM list, joined ones included,
    # and none read only in a subquery.
    match = ROW_LOCK.search(sql.strip())
    if match is None:
        return set()
    if match["of"]:
        return set(re.findall(r'"(\w+)"', match["of"]))
    outer = sql[: match.start()]
    while (stripped := PARENTHESIZED.sub("", outer)) != outer:
        outer = stripped
    return set(re.findall(r'\b(?:FROM|JOIN) "(\w+)"', outer))


def person_target(sql: str, params: Any) -> frozenset[object] | None:
    """The person ids a statement is limited to, or None when its text does not prove them."""
    match = PERSON_FILTER.match(sql.strip())
    if match is None or "(SELECT" in sql or not params:
        return None
    where = match["where"]
    if " OR " in where or "NOT " in where:
        return None
    key = PERSON_KEY.search(where)
    if key is None:
        return None
    start = sql[: match.start("where") + key.start("key")].count("%s")
    return frozenset(params[start : start + key["key"].count("%s")])


@dataclass
class LockOrder:
    locked: set[str] = field(default_factory=set)
    # The statements that, after their transaction locked a workspace row, lock or write
    # person rows the transaction does not provably hold.
    late: list[str] = field(default_factory=list)


@dataclass
class _Transaction:
    outer: object = None
    workspace: bool = False
    # The person ids a statement of this transaction provably locked or wrote.
    persons: set[object] = field(default_factory=set)


@contextmanager
def lock_order() -> Iterator[LockOrder]:
    order = LockOrder()
    state = _Transaction()

    def inspect(
        execute: Callable[..., Any], sql: str, params: Any, many: bool, context: dict[str, Any]
    ) -> Any:
        nonlocal state
        outer = connection.atomic_blocks[0] if connection.atomic_blocks else None
        if outer is None or outer is not state.outer:
            state = _Transaction(outer=outer)
        tables = locked_tables(sql)
        order.locked |= tables
        locks_person = PERSON in tables or PERSON_WRITE.match(sql) is not None
        target = None if many else person_target(sql, params)
        held = target is not None and target <= state.persons
        if locks_person and state.workspace and outer is not None and not held:
            order.late.append(sql)
        result = execute(sql, params, many, context)
        if locks_person and target is not None and context["cursor"].rowcount == len(target):
            state.persons |= target
        state.workspace = state.workspace or bool(tables & WORKSPACE)
        return result

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


STEPS: dict[str, Callable[[User, User, Tenant], object]] = {
    "lock workspace": lambda alice, bob, tenant: list(
        Tenant.objects.select_for_update().filter(pk=tenant.pk)
    ),
    "lock alice by key": lambda alice, bob, tenant: list(
        User.objects.select_for_update().filter(pk=alice.pk)
    ),
    "lock alice by name": lambda alice, bob, tenant: list(
        User.objects.select_for_update().filter(username=alice.username)
    ),
    "lock alice if inactive": lambda alice, bob, tenant: list(
        User.objects.select_for_update().filter(pk=alice.pk, is_active=False)
    ),
    "lock bob by key": lambda alice, bob, tenant: list(
        User.objects.select_for_update().filter(pk=bob.pk)
    ),
    "write alice": lambda alice, bob, tenant: User.objects.filter(pk=alice.pk).update(
        first_name="Renamed"
    ),
    "write bob": lambda alice, bob, tenant: User.objects.filter(pk=bob.pk).update(
        first_name="Renamed"
    ),
    "write alice and bob in bulk": lambda alice, bob, tenant: User.objects.bulk_update(
        [User(pk=alice.pk, first_name="Alice"), User(pk=bob.pk, first_name="Bob")],
        ["first_name"],
    ),
    "delete alice": lambda alice, bob, tenant: User.objects.filter(pk=alice.pk).delete(),
    "delete bob": lambda alice, bob, tenant: User.objects.filter(pk=bob.pk).delete(),
}


@pytest.mark.parametrize(
    ("steps", "late"),
    [
        (("lock workspace", "write alice"), 1),
        (("lock alice by key", "lock workspace", "write alice"), 0),
        (("lock alice by key", "lock workspace", "write bob"), 1),
        (("lock alice by name", "lock workspace", "write bob"), 1),
        (("lock alice by name", "lock workspace", "write alice"), 1),
        (("lock alice if inactive", "lock workspace", "write alice"), 1),
        (("write alice", "lock workspace", "write alice"), 0),
        (("write alice", "lock workspace", "write bob"), 1),
        (("lock alice by key", "lock workspace", "write alice and bob in bulk"), 1),
        (
            (
                "lock alice by key",
                "lock bob by key",
                "lock workspace",
                "write alice and bob in bulk",
            ),
            0,
        ),
        (("lock alice by key", "lock workspace", "lock alice by key"), 0),
        (("lock alice by key", "lock workspace", "lock bob by key"), 1),
        (("lock workspace", "delete alice"), 1),
        (("lock alice by key", "lock workspace", "delete alice"), 0),
        (("lock alice by key", "lock workspace", "delete bob"), 1),
    ],
    ids=lambda value: ", ".join(value) if isinstance(value, tuple) else str(value),
)
def test_a_person_statement_after_a_workspace_lock_is_late_unless_its_rows_were_held(
    steps: tuple[str, ...], late: int
) -> None:
    alice = User.objects.create(username="alice")
    bob = User.objects.create(username="bob")
    tenant = Tenant.objects.create(name="Workspace")
    Membership.objects.create(tenant=tenant, user=alice)

    with lock_order() as order, transaction.atomic():
        for step in steps:
            STEPS[step](alice, bob, tenant)

    assert len(order.late) == late


def test_a_workspace_lock_through_a_person_subquery_locks_no_person() -> None:
    alice = User.objects.create(username="alice")
    tenant = Tenant.objects.create(name="Workspace")
    Membership.objects.create(tenant=tenant, user=alice)
    memberships = Membership.objects.filter(user__username="alice").values("tenant")

    with lock_order() as order, transaction.atomic():
        list(Tenant.objects.select_for_update().filter(pk__in=memberships))

    assert order.locked == {"accounts_tenant"}
