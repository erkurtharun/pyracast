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
from django.db.models import BooleanField, Case, Q, Value, When

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
GUARDED_TABLE = re.compile(
    r'(?<!\w)"?(auth_user|accounts_tenant|regions_region|plants_plant)"?(?!\w)', re.IGNORECASE
)
ANY_ROW_LOCK = re.compile(
    r"\bFOR\s+(?:NO\s+KEY\s+UPDATE|UPDATE|SHARE|KEY\s+SHARE)\b", re.IGNORECASE
)
ANY_READ = re.compile(r"\s*SELECT\b", re.IGNORECASE)
DJANGO_WRITE = re.compile(r'(?:UPDATE|DELETE FROM|INSERT INTO) "(?P<table>\w+)" ')
DJANGO_ROW_LOCK = re.compile(
    r' FOR (?:NO KEY UPDATE|UPDATE|SHARE|KEY SHARE)(?: OF (?P<of>"\w+"(?:, "\w+")*))?'
    r"(?: NOWAIT| SKIP LOCKED)?$"
)
INNERMOST_GROUP = re.compile(r"\([^()]*\)")
COLLAPSED_GROUP = "<group>"
DERIVED_TABLE = re.compile(rf"(?:FROM|JOIN|,) {COLLAPSED_GROUP}")
UNALIASED = r"(?= (?:WHERE|INNER|LEFT|RIGHT|FULL|CROSS|ORDER|GROUP|LIMIT|FOR|ON)\b)"
SAVEPOINT = re.compile(
    r'\s*(?P<verb>SAVEPOINT|ROLLBACK\s+TO(?:\s+SAVEPOINT)?)\s+"?(?P<name>\w+)"?', re.IGNORECASE
)
PERSON_ROW_STATEMENTS = (
    re.compile(
        r'SELECT (?:%s AS "a"|"auth_user"\."\w+"(?: AS "\w+")?'
        r'(?:, "auth_user"\."\w+"(?: AS "\w+")?)*) FROM "auth_user" WHERE (?P<rows>.+?)'
        r'(?: ORDER BY 1 ASC)?(?: LIMIT \d+)? FOR (?:NO KEY )?UPDATE(?: OF "auth_user")?'
    ),
    re.compile(r'UPDATE "auth_user" SET (?:(?!SELECT\b| WHERE ).)+ WHERE (?P<rows>.+)'),
    re.compile(r'DELETE FROM "auth_user" WHERE (?P<rows>.+)'),
)
PERSON_ROWS = re.compile(
    r'"auth_user"\."id" = %s|"auth_user"\."id" IN \(%s(?:, %s)*\)'
    r'|\((?:"auth_user"\."\w+" AND )*"auth_user"\."id" = %s(?: AND "auth_user"\."\w+")*\)'
)


def guarded_tables(sql: str) -> frozenset[str]:
    return frozenset(name.lower() for name in GUARDED_TABLE.findall(sql))


def outer_level(sql: str) -> str:
    while (collapsed := INNERMOST_GROUP.sub(COLLAPSED_GROUP, sql)) != sql:
        sql = collapsed
    return sql


def recognised_acquisitions(sql: str) -> frozenset[str] | None:
    locks = len(ANY_ROW_LOCK.findall(sql))
    if write := DJANGO_WRITE.match(sql):
        return guarded_tables(f'"{write["table"]}"') if locks == 0 else None
    if ANY_READ.match(sql) is None or locks > 1:
        return None
    if locks == 0:
        return frozenset()
    outer = outer_level(sql)
    clause = DJANGO_ROW_LOCK.search(outer)
    if clause is None:
        return None
    if clause["of"]:
        names = re.findall(r'"(\w+)"', clause["of"])
        if all(re.search(rf'(?:FROM|JOIN) "{name}"{UNALIASED}', outer) for name in names):
            return guarded_tables(clause["of"])
        return None
    if DERIVED_TABLE.search(outer):
        return None
    return guarded_tables(outer)


def proven_persons(sql: str, params: Any) -> frozenset[object] | None:
    for statement in PERSON_ROW_STATEMENTS:
        if (match := statement.fullmatch(sql)) and PERSON_ROWS.fullmatch(match["rows"]):
            start = sql[: match.start("rows")].count("%s")
            return frozenset(params[start : start + match["rows"].count("%s")])
    return None


@dataclass
class LockOrder:
    locked: set[str] = field(default_factory=set)
    late: list[str] = field(default_factory=list)


@dataclass
class _Transaction:
    outer: object = None
    workspace: bool = False
    held_persons: set[object] = field(default_factory=set)
    savepoints: dict[str, frozenset[object]] = field(default_factory=dict)

    def follow_savepoint(self, sql: str) -> None:
        if (savepoint := SAVEPOINT.match(sql)) is None:
            return
        if savepoint["verb"].upper() == "SAVEPOINT":
            self.savepoints[savepoint["name"]] = frozenset(self.held_persons)
        else:
            self.held_persons = set(self.savepoints.get(savepoint["name"], ()))


# The detector reads the SQL Django sends, so cascades and checks inside the database are not seen.
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
        recognised = recognised_acquisitions(sql)
        acquired = guarded_tables(sql) if recognised is None else recognised
        proven = None if many else proven_persons(sql, params)
        result = execute(sql, params, many, context)
        rows = context["cursor"].rowcount
        order.locked |= recognised or frozenset()
        takes_new_person_rows = PERSON in acquired and (
            proven is None or (rows > 0 and not proven <= state.held_persons)
        )
        if takes_new_person_rows and state.workspace and outer is not None:
            order.late.append(sql)
        if proven is not None and rows == len(proven):
            state.held_persons |= proven
        state.workspace = state.workspace or bool(acquired & WORKSPACE)
        state.follow_savepoint(sql)
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


def raw(sql: str, *params: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)


def lock_and_roll_back(alice: User) -> None:
    savepoint = transaction.savepoint_create()
    list(User.objects.select_for_update().filter(pk=alice.pk))
    transaction.savepoint_rollback(savepoint)


def unless_named(alice: User) -> Case:
    return Case(
        When(Q(first_name="") & Q(pk=alice.pk) & Q(last_name=""), then=Value(False)),
        default=Value(True),
        output_field=BooleanField(),
    )


STEPS: dict[str, Callable[[User, User, Tenant], object]] = {
    "lock workspace": lambda alice, bob, tenant: list(
        Tenant.objects.select_for_update().filter(pk=tenant.pk)
    ),
    "write workspace": lambda alice, bob, tenant: Tenant.objects.filter(pk=tenant.pk).update(
        name="Renamed"
    ),
    "lock alice while active": lambda alice, bob, tenant: list(
        User.objects.select_for_update(no_key=True).filter(pk=alice.pk, is_active=True)
    ),
    "lock bob by a filter naming alice inside a case": lambda alice, bob, tenant: list(
        User.objects.select_for_update().filter(unless_named(alice), username=bob.username)
    ),
    "lock alice and roll back to a savepoint": lambda alice, bob, tenant: lock_and_roll_back(alice),
    "lock alice in a derived table": lambda alice, bob, tenant: raw(
        'SELECT "locked"."id" FROM (SELECT "auth_user"."id" FROM "auth_user"'
        ' WHERE "auth_user"."id" = %s FOR UPDATE) "locked"',
        alice.pk,
    ),
    "lock alice in a subquery": lambda alice, bob, tenant: raw(
        'SELECT "accounts_tenant"."id" FROM "accounts_tenant" WHERE EXISTS'
        ' (SELECT 1 FROM "auth_user" WHERE "auth_user"."id" = %s FOR UPDATE)',
        alice.pk,
    ),
    "lock alice in raw lower case": lambda alice, bob, tenant: raw(
        "select id from auth_user where id = %s for update", alice.pk
    ),
    "write alice in raw lower case": lambda alice, bob, tenant: raw(
        "update auth_user set first_name = 'Renamed' where id = %s", alice.pk
    ),
    "read alice": lambda alice, bob, tenant: list(User.objects.filter(pk=alice.pk)),
    "read alice in raw lower case": lambda alice, bob, tenant: raw(
        "select id from auth_user where id = %s", alice.pk
    ),
    "lock a membership joined to alice": lambda alice, bob, tenant: list(
        Membership.objects.select_for_update(of=("self",)).filter(user=alice, user__is_active=True)
    ),
    "write a missing person": lambda alice, bob, tenant: User.objects.filter(pk=0).update(
        first_name="Renamed"
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


def late_statements(*steps: str) -> int:
    alice = User.objects.create(username="alice")
    bob = User.objects.create(username="bob")
    tenant = Tenant.objects.create(name="Workspace")
    Membership.objects.create(tenant=tenant, user=alice)

    with lock_order() as order, transaction.atomic():
        for step in steps:
            STEPS[step](alice, bob, tenant)

    return len(order.late)


@pytest.mark.parametrize(
    ("steps", "late"),
    [
        (("lock workspace", "write alice"), 1),
        (("lock alice by key", "lock workspace", "write alice"), 0),
        (("lock alice by key", "lock workspace", "write bob"), 1),
        (("lock alice while active", "lock workspace", "write alice"), 0),
        (("lock alice by name", "lock workspace", "write bob"), 1),
        (("lock alice by name", "lock workspace", "write alice"), 1),
        (("lock alice if inactive", "lock workspace", "write alice"), 1),
        (("lock bob by a filter naming alice inside a case", "lock workspace", "write alice"), 1),
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
def test_a_person_row_is_held_only_after_a_whole_id_filter_matched_it(
    steps: tuple[str, ...], late: int
) -> None:
    assert late_statements(*steps) == late


@pytest.mark.parametrize(
    "step",
    [
        "lock alice in a derived table",
        "lock alice in a subquery",
        "lock alice in raw lower case",
        "write alice in raw lower case",
    ],
)
def test_an_unrecognised_person_lock_or_write_after_a_workspace_lock_is_late(step: str) -> None:
    assert late_statements("lock alice by key", "lock workspace", step) == 1


@pytest.mark.parametrize(
    "step", ["read alice", "read alice in raw lower case", "lock a membership joined to alice"]
)
def test_a_person_read_without_a_person_lock_after_a_workspace_lock_is_not_late(
    step: str,
) -> None:
    assert late_statements("lock workspace", step) == 0


def test_a_workspace_write_orders_a_later_person_write_like_a_workspace_lock() -> None:
    assert late_statements("write workspace", "write alice") == 1


def test_a_rollback_to_a_savepoint_releases_the_person_rows_locked_inside_it() -> None:
    assert (
        late_statements("lock alice and roll back to a savepoint", "lock workspace", "write alice")
        == 1
    )


def test_a_person_write_after_a_workspace_lock_that_matched_no_row_is_not_late() -> None:
    assert late_statements("lock workspace", "write a missing person") == 0


def test_a_workspace_lock_through_a_person_subquery_locks_no_person() -> None:
    alice = User.objects.create(username="alice")
    tenant = Tenant.objects.create(name="Workspace")
    Membership.objects.create(tenant=tenant, user=alice)
    memberships = Membership.objects.filter(user__username="alice").values("tenant")

    with lock_order() as order, transaction.atomic():
        list(Tenant.objects.select_for_update().filter(pk__in=memberships))

    assert order.locked == {"accounts_tenant"}
