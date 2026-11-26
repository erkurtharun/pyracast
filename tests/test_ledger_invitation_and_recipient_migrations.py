import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from django.db import DEFAULT_DB_ALIAS, connection, connections, models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.migrations.executor import MigrationExecutor
from django.db.models import F
from psycopg import sql

from pyracast.accounts.models import (
    Membership,
    Tenant,
    WorkspaceAdminChange,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.change_requests.models import ChangeRequest, IncidentChange
from pyracast.change_requests.operations import reject_request
from pyracast.regions.models import AuditEvent
from tests.support.change_request_boundaries import (
    RequestCase,
    incident_request,
    make_request_case,
    production_request,
)
from tests.support.domain import NOW
from tests.support.invariants import (
    ADMINISTRATOR_CHANGE_RULE,
    AUDIT_DETAILS_RULE,
    INCIDENT_INTERVAL_RULE,
    MEMBERSHIPS_RULE,
    OPEN_INVITATION_RULE,
    RECIPIENTS_RULE,
    REJECTION_REASON_RULE,
    REMINDER_TIMES_RULE,
    REVOKED_RECIPIENTS_RULE,
    violations,
)
from tests.support.migrations import constraints, leaf_nodes, migrate

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = {
    "accounts": ("accounts", "0003_workspace_invitation_role"),
    "change_requests": ("change_requests", "0002_change_vocabulary_checks"),
    "notifications": ("notifications", "0003_delivery_lease_check"),
    "regions": ("regions", "0001_initial"),
}
NEW = {
    "accounts_workspaceadminchange": {"workspaceadminchange_administrator_chk"},
    "accounts_workspaceinvitation": {
        "workspaceinvitation_revoked_at_chk",
        "workspaceinvitation_tenant_email_uniq",
        "workspaceinvitation_tenant_administrator_uniq",
    },
    "change_requests_changerequest": {"changerequest_decision_reason_chk"},
    "change_requests_incidentchange": {"incidentchange_ends_at_chk"},
    "notifications_reminderschedule": {
        "reminderschedule_recipients_chk",
        "reminderschedule_recipient_memberships_chk",
        "reminderschedule_times_chk",
    },
    "notifications_notificationdelivery": {
        "notificationdelivery_recipients_chk",
        "notificationdelivery_recipient_memberships_chk",
        "notificationdelivery_revoked_recipients_chk",
    },
    "regions_auditevent": {"auditevent_details_chk"},
}
# The invitation rule these migrations installed: a revocation ended the deadline.
REVOKED_DEADLINE_RULE = (
    "revoked_at IS NULL OR (accepted_at IS NULL AND NOT expires_at > revoked_at)"
)
RULES = {
    "accounts_workspaceadminchange": (ADMINISTRATOR_CHANGE_RULE,),
    "accounts_workspaceinvitation": (REVOKED_DEADLINE_RULE, OPEN_INVITATION_RULE),
    "change_requests_changerequest": (REJECTION_REASON_RULE,),
    "change_requests_incidentchange": (INCIDENT_INTERVAL_RULE,),
    "notifications_reminderschedule": (RECIPIENTS_RULE, MEMBERSHIPS_RULE, REMINDER_TIMES_RULE),
    "notifications_notificationdelivery": (
        RECIPIENTS_RULE,
        REVOKED_RECIPIENTS_RULE,
        MEMBERSHIPS_RULE,
    ),
    "regions_auditevent": (AUDIT_DETAILS_RULE,),
}
ADDRESS = "recipient@example.com"
WEEK = timedelta(days=7)
START = NOW - timedelta(days=20)
# The invitation link migration expires open invitations and has its own test; the outbound
# mail table follows it through the invitation token migration.
BEFORE_LINK_EXPIRY = {
    "accounts": ("accounts", "0005_schema_naming_convention"),
    "notifications": ("notifications", "0005_schema_naming_convention"),
}


def through_checks() -> list[tuple[str, str]]:
    return [BEFORE_LINK_EXPIRY.get(app, (app, name)) for app, name in leaf_nodes()]


def installed(table: str) -> set[str]:
    return set(constraints(table)) & NEW[table]


def broken_rules() -> dict[str, int]:
    return {
        table: count
        for table, rules in RULES.items()
        if (count := sum(violations(table, rule) for rule in rules))
    }


def previous_invitations() -> models.Manager[models.Model]:
    state = MigrationExecutor(connection).loader.project_state(PREVIOUS["accounts"])
    model: type[models.Model] = state.apps.get_model("accounts", "WorkspaceInvitation")
    return model._default_manager


def invited(
    case: RequestCase, email: str, created_at: datetime, expires_at: datetime, **values: Any
) -> int:
    invitations = previous_invitations()
    row = invitations.create(
        tenant_id=case.system.tenant.pk,
        created_by_id=case.system.member.pk,
        email=email,
        token_hash=uuid4().hex,
        expires_at=expires_at,
        **values,
    )
    invitations.filter(pk=row.pk).update(created_at=created_at)
    return int(row.pk)


def previous_notifications(name: str) -> models.Manager[models.Model]:
    state = MigrationExecutor(connection).loader.project_state(PREVIOUS["notifications"])
    model: type[models.Model] = state.apps.get_model("notifications", name)
    return model._default_manager


def schedule_and_delivery(
    case: RequestCase,
) -> tuple[models.QuerySet[models.Model], models.QuerySet[models.Model]]:
    """Rows written at the previous state, where the recipients were lists."""
    pinned = {ADDRESS: [Membership.objects.get(user=case.proposer).pk]}
    schedules, deliveries = (
        previous_notifications(name) for name in ("ReminderSchedule", "NotificationDelivery")
    )
    schedule = schedules.create(
        region_id=case.system.region.pk,
        effective_from=NOW,
        recipients=[ADDRESS],
        recipient_memberships=pinned,
        times=["06:30", "18:00"],
    )
    delivery = deliveries.create(
        schedule_id=schedule.pk,
        scheduled_for=NOW,
        available_at=NOW,
        binding_id=case.system.binding.pk,
        preparation_deadline=NOW,
        recipients=[ADDRESS],
        recipient_memberships=pinned,
        revoked_recipients=[ADDRESS],
    )
    return schedules.filter(pk=schedule.pk), deliveries.filter(pk=delivery.pk)


def stored(rows: models.QuerySet[Any]) -> list[dict[str, Any]]:
    return list(rows.order_by("pk").values())


def stored_rows(rows: models.QuerySet[Any]) -> list[dict[str, Any]]:
    """The rows under the column names of the migrated state."""
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "SELECT to_jsonb(stored) FROM {} AS stored WHERE id = ANY(%s) ORDER BY id"
            ).format(sql.Identifier(rows.model._meta.db_table)),
            [list(rows.values_list("pk", flat=True))],
        )
        return [json.loads(row) for (row,) in cursor.fetchall()]


@pytest.mark.usefixtures("leaf")
def test_migrations_close_superseded_invitations_and_keep_every_valid_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    member = case.system.member
    rejected = production_request(case)
    reject_request(member, case.system.tenant.pk, rejected.pk, reason="Meter replaced")
    incident_request(case)
    WorkspaceAdminChange.objects.create(
        tenant=case.system.tenant,
        actor=case.system.admin,
        administrator=member,
        previous_administrator=case.proposer,
    )
    kept = {
        rows.model: stored(rows)
        for rows in (
            ChangeRequest.objects.all(),
            IncidentChange.objects.all(),
            WorkspaceAdminChange.objects.all(),
        )
    }
    migrate(*PREVIOUS.values())
    assert all(installed(table) == set() for table in NEW)
    reminders = schedule_and_delivery(case)
    reminder_rows = [stored_rows(rows) for rows in reminders]
    # Main's writers end an invitation by moving its deadline, at the latest when a newer
    # invitation of its address, or of the administrator role, is created.
    replaced_at = START + timedelta(days=2)
    superseded = invited(case, "a@example.com", START, replaced_at)
    successor = invited(case, "a@example.com", replaced_at, replaced_at + WEEK)
    lapsed = invited(case, "b@example.com", START, START + WEEK)
    renewed = invited(case, "b@example.com", NOW - timedelta(days=1), NOW + WEEK)
    first_administrator = invited(
        case, "c@example.com", START, replaced_at, role=WorkspaceRole.ADMIN
    )
    administrator = invited(
        case, "d@example.com", replaced_at, NOW + WEEK, role=WorkspaceRole.ADMIN
    )
    accepted = invited(case, "a@example.com", START, START + WEEK, accepted_at=START)
    alone = invited(case, "e@example.com", START, START + timedelta(days=1))
    invitations = stored(previous_invitations().all())

    migrate(*through_checks())

    assert {table: installed(table) for table in NEW} == NEW
    assert broken_rules() == {}
    assert stored(previous_invitations().all()) == invitations
    assert dict(WorkspaceInvitation.objects.values_list("pk", "revoked_at")) == {
        superseded: replaced_at,
        successor: None,
        lapsed: START + WEEK,
        renewed: None,
        first_administrator: replaced_at,
        administrator: None,
        accepted: None,
        alone: None,
    }
    assert {model: stored(model._default_manager.all()) for model in kept} == kept
    assert [stored_rows(rows) for rows in reminders] == reminder_rows
    assert AuditEvent.objects.exists()
    assert not AuditEvent.objects.exclude(details={}).exists()

    migrate(*PREVIOUS.values())
    assert all(installed(table) == set() for table in NEW)


@pytest.mark.usefixtures("leaf")
def test_migration_uses_the_earliest_successor_time_in_primary_key_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    migrate(*PREVIOUS.values())
    older = invited(case, ADDRESS, START, START + timedelta(days=4))
    middle = invited(case, ADDRESS, START + timedelta(days=5), START + timedelta(days=3))
    latest = invited(case, ADDRESS, START + timedelta(days=3), START + WEEK)
    invitations = previous_invitations()
    other = invitations.create(
        tenant_id=Tenant.objects.create(name="Other workspace").pk,
        created_by_id=case.system.member.pk,
        email=ADDRESS,
        token_hash=uuid4().hex,
        expires_at=START + WEEK,
    )
    invitations.filter(pk=other.pk).update(created_at=START + timedelta(days=2))

    with pytest.raises(ValueError, match=r"holds 1 row.*still open"):
        migrate(*through_checks())

    assert invitations.values_list("expires_at", flat=True).get(pk=older) == START + timedelta(
        days=4
    )
    invitations.filter(pk=older).update(expires_at=START + timedelta(days=3))
    migrate(*through_checks())

    assert dict(WorkspaceInvitation.objects.values_list("pk", "revoked_at")) == {
        older: START + timedelta(days=3),
        middle: START + timedelta(days=3),
        latest: None,
        other.pk: None,
    }
    assert broken_rules() == {}


def overlapping_invitations(case: RequestCase) -> models.QuerySet[Any]:
    older = invited(case, "a@example.com", START, START + WEEK)
    invited(case, "a@example.com", START + timedelta(days=1), START + WEEK)
    return previous_invitations().filter(pk=older)


def unchanged_administrator(case: RequestCase) -> models.QuerySet[Any]:
    change = WorkspaceAdminChange.objects.create(
        tenant=case.system.tenant,
        actor=case.system.admin,
        administrator=case.system.member,
        previous_administrator=case.system.member,
    )
    return WorkspaceAdminChange.objects.filter(pk=change.pk)


def rejected_without_reason(case: RequestCase) -> models.QuerySet[Any]:
    rows = ChangeRequest.objects.filter(plant=case.plant, status="rejected")
    rows.update(decision_reason="")
    return rows


def incident_ending_at_its_start(case: RequestCase) -> models.QuerySet[Any]:
    rows = IncidentChange.objects.filter(request__plant=case.plant)
    rows.update(ends_at=F("starts_at"))
    return rows


def schedule_shape(**changes: object) -> Callable[[RequestCase], models.QuerySet[Any]]:
    def write(case: RequestCase) -> models.QuerySet[Any]:
        rows, _ = schedule_and_delivery(case)
        rows.update(**changes)
        return rows

    return write


def delivery_shape(**changes: object) -> Callable[[RequestCase], models.QuerySet[Any]]:
    def write(case: RequestCase) -> models.QuerySet[Any]:
        _, rows = schedule_and_delivery(case)
        rows.update(**changes)
        return rows

    return write


@pytest.mark.usefixtures("leaf")
@pytest.mark.parametrize(
    ("write", "table", "refusal", "correction"),
    [
        (
            overlapping_invitations,
            "accounts_workspaceinvitation",
            r"accounts_workspaceinvitation holds 1 row.*still open",
            {"expires_at": START + timedelta(days=1)},
        ),
        (
            unchanged_administrator,
            "accounts_workspaceadminchange",
            r"accounts_workspaceadminchange holds 1 row.*names its administrator",
            {"previous_administrator": None},
        ),
        (
            rejected_without_reason,
            "change_requests_changerequest",
            r"change_requests_changerequest holds 1 row.*reason",
            {"decision_reason": "Restored reason"},
        ),
        (
            incident_ending_at_its_start,
            "change_requests_incidentchange",
            r"change_requests_incidentchange holds 1 row.*ends after",
            {"ends_at": None},
        ),
        (
            schedule_shape(recipients={ADDRESS: True}),
            "notifications_reminderschedule",
            r"notifications_reminderschedule holds 1 row.*recipients",
            {"recipients": [ADDRESS]},
        ),
        (
            schedule_shape(times=["6:30"]),
            "notifications_reminderschedule",
            r"notifications_reminderschedule holds 1 row.*times",
            {"times": ["06:30"]},
        ),
        (
            schedule_shape(recipient_memberships={ADDRESS: 1}),
            "notifications_reminderschedule",
            r"notifications_reminderschedule holds 1 row.*memberships",
            {"recipient_memberships": {}},
        ),
        (
            delivery_shape(recipient_memberships={ADDRESS: ["1"]}),
            "notifications_notificationdelivery",
            r"notifications_notificationdelivery holds 1 row.*memberships",
            {"recipient_memberships": {}},
        ),
        (
            delivery_shape(revoked_recipients=ADDRESS),
            "notifications_notificationdelivery",
            r"notifications_notificationdelivery holds 1 row.*revoked",
            {"revoked_recipients": []},
        ),
        (
            delivery_shape(recipients=[None]),
            "notifications_notificationdelivery",
            r"notifications_notificationdelivery holds 1 row.*recipients",
            {"recipients": []},
        ),
    ],
    ids=[
        "overlapping_invitations",
        "administrator_unchanged",
        "rejection_without_reason",
        "incident_interval",
        "schedule_recipients",
        "schedule_times",
        "schedule_memberships",
        "delivery_memberships",
        "delivery_revoked",
        "delivery_recipients",
    ],
)
def test_migrations_refuse_rows_no_writer_stores_and_change_nothing(
    write: Callable[[RequestCase], models.QuerySet[Any]],
    table: str,
    refusal: str,
    correction: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    # The current models write change requests only under the current column names.
    rejected = production_request(case)
    reject_request(case.system.member, case.system.tenant.pk, rejected.pk, reason="Wrong")
    incident_request(case)
    migrate(*PREVIOUS.values())
    rows = write(case)
    kept = stored_rows(rows)

    with pytest.raises(ValueError, match=refusal):
        migrate(*through_checks())

    assert installed(table) == set()
    assert stored_rows(rows) == kept
    rows.update(**correction)
    migrate(*through_checks())
    assert installed(table) == NEW[table]
    assert broken_rules() == {}


def at_leaf(database: BaseDatabaseWrapper) -> bool:
    with database.cursor() as cursor:
        cursor.execute("SELECT app, name FROM django_migrations")
        return set(leaf_nodes()) <= set(cursor.fetchall())


def refuses_stored_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    case = make_request_case(monkeypatch)
    migrate(*PREVIOUS.values())
    schedule_shape(recipient_memberships={ADDRESS: 1})(case)
    migrate(*through_checks())


def leaves_a_transaction_open_after_a_downgrade() -> None:
    migrate(*PREVIOUS.values())
    connection.set_autocommit(False)
    assert at_leaf(connection)


class Phases:
    def __init__(self) -> None:
        self.reported: list[tuple[str | None, str]] = []

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.reported.append((report.when, report.outcome))


def run_with_leaf(body: Callable[..., None], folder: Path) -> list[tuple[str | None, str]]:
    module = folder / f"test_{body.__name__}.py"
    module.write_text(
        "import pytest\n"
        "from tests.conftest import leaf\n"
        f"from {__name__} import {body.__name__} as test_body\n"
        'pytestmark = pytest.mark.usefixtures("leaf")\n',
        encoding="utf-8",
    )
    settings = folder / "pytest.ini"
    settings.write_text("[pytest]\n", encoding="utf-8")
    phases = Phases()
    pytest.main(
        [
            str(module),
            f"--config-file={settings}",
            "--import-mode=importlib",
            *("-p", "no:django", "-p", "no:xdist", "-p", "no:xdist.looponfail"),
            *("-p", "no:pytest_cov", "-p", "no:cacheprovider", "-p", "no:faulthandler"),
        ],
        plugins=[phases],
    )
    return phases.reported


@pytest.mark.usefixtures("leaf")
@pytest.mark.parametrize(
    "body",
    [refuses_stored_rows, leaves_a_transaction_open_after_a_downgrade],
    ids=["refused rows", "open transaction"],
)
def test_the_leaf_fixture_restores_the_latest_schema_after_a_failing_body(
    body: Callable[..., None], tmp_path: Path
) -> None:
    assert run_with_leaf(body, tmp_path) == [
        ("setup", "passed"),
        ("call", "failed"),
        ("teardown", "passed"),
    ]
    fresh = connections.create_connection(DEFAULT_DB_ALIAS)
    try:
        assert at_leaf(fresh)
    finally:
        fresh.close()
