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
from django.db.models.expressions import RawSQL
from django.test import Client
from psycopg.pq import TransactionStatus

from pyracast.accounts.invitations import accept_workspace_invitation, invitation_digest
from pyracast.accounts.models import Membership, Tenant, WorkspaceInvitation, WorkspaceRole
from pyracast.accounts.operations import remove_workspace_member
from pyracast.accounts.retention import reclaim_signup
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
from tests.support.notifications import SLOT, chosen
from tests.support.plant_operations import request_run

pytestmark = pytest.mark.django_db(transaction=True)

PERSON = "auth_user"
WORKSPACE = frozenset({"accounts_tenant", "regions_region", "plants_plant"})
GUARDED = WORKSPACE | {PERSON}
GUARDED_TABLE = re.compile(
    r"(?<![\w$])(auth_user|accounts_tenant|regions_region|plants_plant)(?![\w$])"
)
ESCAPED_NAME = 'u&"'
ACQUIRING_WORD = re.compile(
    r"(?<![\w$])(?:insert|update|delete|merge|truncate|copy|lock|share)(?![\w$])"
)
LATER_STATEMENT = re.compile(r";\s*\S")
SAVEPOINT_WORD = re.compile(r"(?<![\w$])(?:savepoint|release|rollback)(?![\w$])")
SAVEPOINT_CONTROL = re.compile(
    r"\s*(?P<verb>savepoint|release(?:\s+savepoint)?"
    r"|rollback(?:\s+(?:work|transaction))?\s+to(?:\s+savepoint)?)"
    r'\s+(?:"(?P<quoted>[^"]+)"|(?P<plain>\w+))\s*;?\s*',
    re.IGNORECASE,
)
PERSON_ROW_STATEMENTS = (
    re.compile(
        r'SELECT (?:%s AS "a"|"auth_user"\."\w+"(?: AS "\w+")?'
        r'(?:, "auth_user"\."\w+"(?: AS "\w+")?)*) FROM "auth_user" WHERE (?P<rows>.+?)'
        r'(?: ORDER BY 1 ASC)?(?: LIMIT \d+)? FOR (?:NO KEY )?UPDATE(?: OF "auth_user")?'
    ),
    re.compile(r'UPDATE "auth_user" SET "\w+" = %s(?:, "\w+" = %s)* WHERE (?P<rows>.+)'),
    re.compile(r'DELETE FROM "auth_user" WHERE (?P<rows>.+)'),
)
MEMBERSHIP_ONLY_LOCK = re.compile(
    r'SELECT "accounts_membership"\."\w+"(?:, "accounts_membership"\."\w+")*'
    r' FROM "accounts_membership" INNER JOIN "auth_user"'
    r' ON \("accounts_membership"\."user_id" = "auth_user"\."id"\)'
    r' WHERE \((?:"accounts_membership"\."\w+" = %s|"auth_user"\."is_active")'
    r'(?: AND (?:"accounts_membership"\."\w+" = %s|"auth_user"\."is_active"))*\)'
    r'(?: ORDER BY "accounts_membership"\."id" ASC)?(?: LIMIT \d+)?'
    r' FOR UPDATE OF "accounts_membership"'
)
PERSON_ROWS = re.compile(
    r'"auth_user"\."id" = %s|"auth_user"\."id" IN \(%s(?:, %s)*\)'
    r'|\((?:"auth_user"\."\w+" AND )*"auth_user"\."id" = %s(?: AND "auth_user"\."\w+")*\)'
)


def acquired_tables(sql: str) -> frozenset[str]:
    text = sql.casefold()
    if ACQUIRING_WORD.search(text) is None or MEMBERSHIP_ONLY_LOCK.fullmatch(sql):
        return frozenset()
    if ESCAPED_NAME in text:
        return GUARDED
    return frozenset(GUARDED_TABLE.findall(text))


def proven_persons(sql: str, params: Any, rows: int) -> frozenset[object] | None:
    if "%" in sql.replace("%s", ""):
        return None
    for statement in PERSON_ROW_STATEMENTS:
        if (match := statement.fullmatch(sql)) and PERSON_ROWS.fullmatch(match["rows"]):
            start = sql[: match.start("rows")].count("%s")
            ids = frozenset(params[start : start + match["rows"].count("%s")])
            return ids if rows == len(ids) else None
    return None


@dataclass
class LockOrder:
    locked: set[str] = field(default_factory=set)
    late: list[str] = field(default_factory=list)


@dataclass
class _Transaction:
    workspace: bool = False
    open_person_cursor: bool = False
    held_persons: frozenset[object] = frozenset()
    savepoints: list[tuple[str, frozenset[object]]] = field(default_factory=list)

    def follow_savepoint(self, sql: str) -> None:
        if SAVEPOINT_WORD.search(sql.casefold()) is None:
            return
        if (control := SAVEPOINT_CONTROL.fullmatch(sql)) is None:
            self.forget_held_persons()
            return
        verb = control["verb"].split()[0].lower()
        name = control["quoted"] or control["plain"].lower()
        names = [saved for saved, _held in self.savepoints]
        if verb == "savepoint":
            self.savepoints.append((name, self.held_persons))
        elif name not in names:
            self.forget_held_persons()
        else:
            latest = len(names) - 1 - names[::-1].index(name)
            if verb == "rollback":
                self.held_persons = self.savepoints[latest][1]
                latest += 1
            del self.savepoints[latest:]

    def forget_held_persons(self) -> None:
        self.held_persons, self.savepoints = frozenset(), []


# The detector reads the SQL Django sends; cascades, checks and views in the database are unseen.
@contextmanager
def lock_order() -> Iterator[LockOrder]:
    order = LockOrder()
    state = _Transaction()

    def inspect(
        execute: Callable[..., Any], sql: str, params: Any, many: bool, context: dict[str, Any]
    ) -> Any:
        nonlocal state
        if connection.connection.info.transaction_status is TransactionStatus.IDLE:
            state = _Transaction()
        result = execute(sql, params, many, context)
        several = LATER_STATEMENT.search(sql) is not None
        rows = -1 if several else context["cursor"].rowcount
        acquired = acquired_tables(sql)
        proven = None if many else proven_persons(sql, params, rows)
        if proven is not None:
            order.locked.add(PERSON)
        if rows > 0:
            order.locked |= acquired & WORKSPACE
        already_held = proven is not None and proven <= state.held_persons
        workspace = bool(acquired & WORKSPACE)
        late_person = PERSON in acquired and (state.workspace or workspace) and not already_held
        late_workspace = workspace and state.open_person_cursor
        if late_person or late_workspace:
            order.late.append(sql)
        if proven is not None:
            state.held_persons |= proven
        state.workspace = state.workspace or workspace
        state.open_person_cursor = state.open_person_cursor or (rows < 0 and PERSON in acquired)
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


def reminder_save(system: System) -> Callable[[], object]:
    membership = Membership.objects.get(user=system.member).pk
    return lambda: configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["ops@example.com"],
        include_workspace_members=True,
        times=["10:00"],
        language="en",
        now=NOW,
        hide_recipients=False,
        member_roles=[MemberChoice(frozenset({membership}), system.member.email, RecipientRole.CC)],
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


def raw(sql: str, *params: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)


def lock_and_roll_back(person: User) -> None:
    savepoint = transaction.savepoint_create()
    list(User.objects.select_for_update().filter(pk=person.pk))
    transaction.savepoint_rollback(savepoint)


def lock_and_reuse_a_savepoint_name(alice: User) -> None:
    raw("SAVEPOINT s")
    list(User.objects.select_for_update().filter(pk=alice.pk))
    raw("SAVEPOINT s")
    raw("RELEASE SAVEPOINT s")
    raw("ROLLBACK TO SAVEPOINT s")


def fetch_alice_lock_workspace_fetch_bob(alice: User, bob: User, tenant: Tenant) -> None:
    persons = (
        User.objects.filter(pk__in=(alice.pk, bob.pk))
        .order_by("pk")
        .select_for_update()
        .iterator(chunk_size=1)
    )
    next(persons)
    list(Tenant.objects.select_for_update().filter(pk=tenant.pk))
    next(persons)


def unless_named(alice: User) -> Case:
    return Case(
        When(Q(first_name="") & Q(pk=alice.pk) & Q(last_name=""), then=Value(False)),
        default=Value(True),
        output_field=BooleanField(),
    )


def lock_a_membership_joined_to(alice: User, respell: Callable[[str], str]) -> None:
    memberships = Membership.objects.select_for_update(of=("self",))
    sql, params = memberships.filter(user=alice, user__is_active=True).query.sql_with_params()
    raw(respell(sql), *params)


def aliased_person(sql: str) -> str:
    return sql.replace('INNER JOIN "auth_user" ON', 'INNER JOIN "auth_user" "person" ON').replace(
        '"auth_user"."', '"person"."'
    )


def locked_person(sql: str) -> str:
    return sql.replace('OF "accounts_membership"', 'OF "accounts_membership", "auth_user"')


def joined_tenant(sql: str) -> str:
    return sql.replace(
        " WHERE (",
        ' INNER JOIN "accounts_tenant"'
        ' ON ("accounts_membership"."tenant_id" = "accounts_tenant"."id") WHERE (',
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
    "lock bob and roll back to a savepoint": lambda alice, bob, tenant: lock_and_roll_back(bob),
    "lock alice and reuse a savepoint name": lambda alice, bob, tenant: (
        lock_and_reuse_a_savepoint_name(alice)
    ),
    "lock bob through an iterator": lambda alice, bob, tenant: list(
        User.objects.filter(pk=bob.pk).select_for_update().iterator()
    ),
    "fetch alice, lock workspace and fetch bob through one iterator": (
        fetch_alice_lock_workspace_fetch_bob
    ),
    "lock bob in a lateral derived table": lambda alice, bob, tenant: raw(
        "SELECT p.id FROM LATERAL (SELECT id FROM auth_user WHERE id = %s) AS p FOR UPDATE",
        bob.pk,
    ),
    "lock bob behind an empty statement": lambda alice, bob, tenant: raw(
        "SELECT 1 WHERE false; SELECT id FROM auth_user WHERE id = %s FOR UPDATE", bob.pk
    ),
    "lock workspace and bob in one execution": lambda alice, bob, tenant: raw(
        "SELECT id FROM accounts_tenant WHERE id = %s FOR UPDATE;"
        " SELECT id FROM auth_user WHERE id = %s FOR UPDATE",
        tenant.pk,
        bob.pk,
    ),
    "lock memberships with their workspace and persons": lambda alice, bob, tenant: list(
        Membership.objects.filter(tenant=tenant)
        .select_related("tenant", "user")
        .order_by("user_id")
        .select_for_update(of=("tenant", "user"))
    ),
    "lock bob with a comment inside the locking clause": lambda alice, bob, tenant: raw(
        "SELECT id FROM auth_user WHERE id = %s FOR /* row lock */ UPDATE", bob.pk
    ),
    "write bob with alice's id in a text placeholder": lambda alice, bob, tenant: (
        User.objects.filter(pk=bob.pk).update(first_name=RawSQL("CAST(%t AS text)", (alice.pk,)))
    ),
    "write alice with a subquery locking bob": lambda alice, bob, tenant: User.objects.filter(
        pk=alice.pk
    ).update(
        first_name=RawSQL("(select username from auth_user where id = %s for update)", (bob.pk,))
    ),
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
    "lock a membership and alice joined to it": lambda alice, bob, tenant: (
        lock_a_membership_joined_to(alice, locked_person)
    ),
    "lock a membership joined to alice without a target list": lambda alice, bob, tenant: list(
        Membership.objects.select_for_update().filter(user=alice, user__is_active=True)
    ),
    "lock alice joined to her membership": lambda alice, bob, tenant: list(
        User.objects.select_for_update(of=("self",)).filter(
            membership__tenant=tenant, membership__user=alice, is_active=True
        )
    ),
    "lock a membership joined to an aliased alice": lambda alice, bob, tenant: (
        lock_a_membership_joined_to(alice, aliased_person)
    ),
    "lock a membership joined to alice in lower case": lambda alice, bob, tenant: (
        lock_a_membership_joined_to(alice, str.lower)
    ),
    "lock a membership joined to alice and its workspace": lambda alice, bob, tenant: (
        lock_a_membership_joined_to(alice, joined_tenant)
    ),
    "write a missing person": lambda alice, bob, tenant: User.objects.filter(pk=0).update(
        first_name="Renamed"
    ),
    "write a missing person by name": lambda alice, bob, tenant: User.objects.filter(
        username="nobody"
    ).update(first_name="Renamed"),
    "lock a missing person": lambda alice, bob, tenant: list(
        User.objects.select_for_update().filter(pk=0)
    ),
    "write a missing person through a subquery locking bob": lambda alice, bob, tenant: (
        User.objects.filter(
            username="nobody",
            pk=RawSQL("(select id from auth_user where id = %s for update)", (bob.pk,)),
        ).update(first_name="Renamed")
    ),
    "lock bob through a viewer membership, skipping locked rows": lambda alice, bob, tenant: list(
        User.objects.select_for_update(skip_locked=True).filter(
            pk=bob.pk, membership__role=WorkspaceRole.VIEWER
        )
    ),
    "lock bob on an empty page": lambda alice, bob, tenant: list(
        User.objects.select_for_update().filter(pk=bob.pk)[1:2]
    ),
    "lock bob through an upsert that updates nothing": lambda alice, bob, tenant: raw(
        "INSERT INTO auth_user (password, is_superuser, username, first_name, last_name, email,"
        " is_staff, is_active, date_joined) VALUES ('', false, 'bob', '', '', '', false, true,"
        " now()) ON CONFLICT (username) DO UPDATE SET first_name = 'Renamed' WHERE false"
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
    "lock bob while active": lambda alice, bob, tenant: list(
        User.objects.select_for_update().filter(pk=bob.pk, is_active=True)
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


def alice_bob_and_workspace() -> tuple[User, User, Tenant]:
    alice = User.objects.create(username="alice")
    bob = User.objects.create(username="bob")
    tenant = Tenant.objects.create(name="Workspace")
    Membership.objects.create(tenant=tenant, user=alice)
    return alice, bob, tenant


@transaction.atomic
def in_one_transaction(people: tuple[User, User, Tenant], *steps: str) -> None:
    for step in steps:
        STEPS[step](*people)


def detected(*steps: str) -> LockOrder:
    people = alice_bob_and_workspace()

    with lock_order() as order:
        in_one_transaction(people, *steps)

    return order


def late_statements(*steps: str) -> int:
    return len(detected(*steps).late)


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
        (("write bob with alice's id in a text placeholder", "lock workspace", "write alice"), 1),
        (("lock alice by key", "lock workspace", "write alice with a subquery locking bob"), 1),
        (("lock alice by key", "lock workspace", "write alice and bob in bulk"), 1),
        (
            (
                "lock alice by key",
                "lock bob by key",
                "lock workspace",
                "write alice and bob in bulk",
            ),
            1,
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
        "lock a membership and alice joined to it",
        "lock a membership joined to alice without a target list",
        "lock alice joined to her membership",
        "lock a membership joined to an aliased alice",
        "lock a membership joined to alice in lower case",
        "lock a membership joined to alice and its workspace",
        "lock bob through an iterator",
        "lock bob in a lateral derived table",
        "lock bob with a comment inside the locking clause",
        "lock bob behind an empty statement",
        "lock bob on an empty page",
        "lock bob through an upsert that updates nothing",
        "write a missing person through a subquery locking bob",
        "write a missing person",
        "write a missing person by name",
        "lock a missing person",
        "lock bob through a viewer membership, skipping locked rows",
    ],
)
def test_an_unproven_person_lock_or_write_after_a_workspace_lock_is_late(step: str) -> None:
    assert late_statements("lock alice by key", "lock workspace", step) == 1


def test_a_person_locked_in_a_committed_transaction_is_not_held_in_the_next() -> None:
    people = alice_bob_and_workspace()

    with lock_order() as order:
        in_one_transaction(people, "lock alice by key")
        in_one_transaction(people, "lock workspace", "write alice")

    assert len(order.late) == 1


def test_a_transaction_opened_by_turning_autocommit_off_is_checked_like_an_atomic_block() -> None:
    alice, bob, tenant = alice_bob_and_workspace()
    transaction.set_autocommit(False)
    try:
        with lock_order() as order:
            STEPS["lock workspace"](alice, bob, tenant)
            STEPS["lock bob by key"](alice, bob, tenant)
        transaction.commit()
    finally:
        transaction.set_autocommit(True)

    assert len(order.late) == 1


def test_a_person_write_after_a_workspace_write_in_autocommit_is_not_late() -> None:
    alice, bob, tenant = alice_bob_and_workspace()

    with lock_order() as order:
        STEPS["write workspace"](alice, bob, tenant)
        STEPS["write alice"](alice, bob, tenant)

    assert order.late == []


@pytest.mark.parametrize(
    "step",
    [
        "lock workspace and bob in one execution",
        "lock memberships with their workspace and persons",
    ],
)
def test_one_execution_acquiring_workspace_and_person_rows_is_late(step: str) -> None:
    assert late_statements("lock alice by key", step) == 1


def test_a_workspace_lock_while_a_person_locking_cursor_is_open_is_late() -> None:
    assert (
        late_statements(
            "lock alice by key", "fetch alice, lock workspace and fetch bob through one iterator"
        )
        == 1
    )


@pytest.mark.parametrize("step", ["read alice", "read alice in raw lower case"])
def test_a_person_read_without_a_lock_after_a_workspace_lock_is_not_late(step: str) -> None:
    assert late_statements("lock workspace", step) == 0


def test_the_membership_lock_joined_to_a_person_acquires_no_person() -> None:
    order = detected("lock workspace", "lock a membership joined to alice")

    assert order.late == []
    assert PERSON not in order.locked


def test_a_workspace_write_orders_a_later_person_write_like_a_workspace_lock() -> None:
    assert late_statements("write workspace", "write alice") == 1


@pytest.mark.parametrize(
    "step", ["lock alice and roll back to a savepoint", "lock alice and reuse a savepoint name"]
)
def test_a_rollback_to_a_savepoint_releases_the_person_rows_locked_inside_it(step: str) -> None:
    assert late_statements(step, "lock workspace", "write alice") == 1


def test_a_rollback_to_a_savepoint_keeps_the_person_rows_locked_before_it() -> None:
    assert (
        late_statements(
            "lock alice by key",
            "lock bob and roll back to a savepoint",
            "lock workspace",
            "write alice",
        )
        == 0
    )


def test_a_person_lock_that_reports_no_row_after_a_workspace_lock_is_late() -> None:
    alice, bob, tenant = alice_bob_and_workspace()
    User.objects.filter(pk=bob.pk).update(is_active=False)

    with lock_order() as order:
        in_one_transaction(
            (alice, bob, tenant), "lock alice by key", "lock workspace", "lock bob while active"
        )

    assert len(order.late) == 1


def test_a_person_write_that_matched_no_row_is_no_evidence_of_a_person_lock() -> None:
    assert PERSON not in detected("lock workspace", "write a missing person").locked


def test_a_workspace_lock_through_a_person_subquery_is_no_evidence_of_a_person_lock() -> None:
    alice = User.objects.create(username="alice")
    tenant = Tenant.objects.create(name="Workspace")
    Membership.objects.create(tenant=tenant, user=alice)
    memberships = Membership.objects.filter(user__username="alice").values("tenant")

    with lock_order() as order, transaction.atomic():
        list(Tenant.objects.select_for_update().filter(pk__in=memberships))

    assert order.locked == {"accounts_tenant"}
