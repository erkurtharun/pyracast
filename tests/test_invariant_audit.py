import math
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from _pytest.mark import ParameterSet
from django.db import DataError, IntegrityError, connection, models, transaction
from django.db.models import F

from pyracast.accounts.models import OutboundMail, WorkspaceAdminChange, WorkspaceInvitation
from pyracast.change_requests.models import ChangeRequest, IncidentChange
from pyracast.change_requests.operations import reject_request
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    HistoricalReferenceImport,
    ReferenceCapacityNormalization,
)
from pyracast.metering.models import EpiasCollectionJob
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.plants.models import CalibrationRun, Plant, PlantConfiguration, ProductionObservation
from pyracast.regions.models import AuditEvent
from tests.support.capacity_normalization import normalize, scaled_system
from tests.support.change_request_boundaries import (
    incident_request,
    make_request_case,
    production_request,
)
from tests.support.domain import NOW, FakeAdapter, System, make_system
from tests.support.invariants import (
    ADMINISTRATOR_CHANGE_RULE,
    AUDIT_DETAILS_RULE,
    BINDING_REVISION_RULE,
    CALIBRATION_RUN_RULE,
    IMPORT_SHAPE_RULE,
    INCIDENT_INTERVAL_RULE,
    INVITATION_ACCEPTOR_RULE,
    INVITATION_STATE_RULE,
    LEASE_RULES,
    MAIL_CONTEXT_RULE,
    MAIL_INVITATION_RULE,
    NATIVE_SLOT_RULE,
    NORMALIZATION_FACTOR_RULE,
    OBSERVATION_LINEAGE_RULE,
    OPEN_INVITATION_RULE,
    PLANT_AUTOMATION_RULE,
    PROVIDER_ACCOUNT_RULE,
    REJECTION_REASON_RULE,
    REMINDER_TIMES_RULE,
    SEALED_CODE_RULE,
    SITE_BINDING_RULE,
    STORED_RULES,
    stored_violations,
)
from tests.support.metering import PROVIDER_PLANT, FixtureClient, connected
from tests.support.plant_operations import make_plant, request_run
from tests.support.plant_training import actual_snapshot_for
from tests.support.trash_operations import sibling_plant

pytestmark = pytest.mark.django_db

type Rule = tuple[str, str]
type Rows = models.QuerySet[Any]
type Write = Callable[[], object]
type Stage = Callable[[pytest.MonkeyPatch], Write]

TOKEN = uuid.uuid4()
UNAVAILABLE = {
    "format": "plain",
    "kind": "unavailable",
    "subject": "Pyracast forecast unavailable",
    "body": "Tomorrow's saved forecast could not be prepared.",
    "prepared_at": NOW.isoformat(),
}


@pytest.fixture(autouse=True)
def providers_answer_inside_the_case_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    # The provider doubles refuse a caller inside a transaction; these cases stage rows in one.
    monkeypatch.setattr(FakeAdapter, "validate", lambda self, binding: None)
    monkeypatch.setattr(FixtureClient, "list_plants", lambda self: (PROVIDER_PLANT,))


@dataclass(frozen=True)
class Guard:
    lift: str
    refusal: str


def check(table: str, name: str) -> Guard:
    return Guard(f"ALTER TABLE {table} DROP CONSTRAINT {name}", name)


def unique_index(name: str) -> Guard:
    return Guard(f"DROP INDEX {name}", name)


def row_trigger(table: str, name: str, message: str) -> Guard:
    return Guard(f"ALTER TABLE {table} DISABLE TRIGGER {name}", message)


@dataclass(frozen=True)
class Breach:
    rule: Rule
    stage: Stage
    guard: Guard
    rows: int = 1


def breach(name: str, rule: Rule, stage: Stage, guard: Guard, rows: int = 1) -> ParameterSet:
    return pytest.param(Breach(rule, stage, guard, rows), id=f"{rule[0]}-{name}")


def changing(stored: Callable[[pytest.MonkeyPatch], Rows], **values: object) -> Stage:
    def stage(monkeypatch: pytest.MonkeyPatch) -> Write:
        rows = stored(monkeypatch)
        return lambda: rows.update(**values)

    return stage


def collection_job(monkeypatch: pytest.MonkeyPatch) -> Rows:
    system = make_system()
    job = CollectionJob.objects.create(
        binding=system.binding, scheduled_for=NOW, target_date=NOW.date(), available_at=NOW
    )
    return CollectionJob.objects.filter(pk=job.pk)


def reference_import_job(monkeypatch: pytest.MonkeyPatch) -> Rows:
    system = make_system()
    record = HistoricalReferenceImport.objects.create(
        binding=system.binding,
        content_sha256="a" * 64,
        source_name="fixture",
        source_settings={},
        imported_by=system.admin,
        created_at=NOW,
        interval_count=1,
    )
    job = CollectionJob.objects.create(
        binding=system.binding,
        reference_import=record,
        endpoint="historical_import",
        scheduled_for=NOW,
        target_date=NOW.date(),
        available_at=NOW,
        status="succeeded",
    )
    return CollectionJob.objects.filter(pk=job.pk)


def epias_job(monkeypatch: pytest.MonkeyPatch) -> Rows:
    connected(monkeypatch)
    return EpiasCollectionJob.objects.filter(pk=EpiasCollectionJob.objects.earliest("pk").pk)


def calibration_run(monkeypatch: pytest.MonkeyPatch) -> Rows:
    run = request_run(*make_plant(monkeypatch))
    return CalibrationRun.objects.filter(pk=run.pk)


def finished_run(monkeypatch: pytest.MonkeyPatch) -> Rows:
    system, plant = make_plant(monkeypatch)
    run = CalibrationRun.objects.create(
        plant=plant,
        configuration=PlantConfiguration.objects.get(plant=plant),
        binding=system.binding,
        requested_by=system.member,
        window_start=NOW.date() - timedelta(days=7),
        window_end=NOW.date() - timedelta(days=1),
        cutoff_at=NOW,
        status="insufficient",
        assessment="insufficient",
        completed_at=NOW,
    )
    return CalibrationRun.objects.filter(pk=run.pk)


def delivery(monkeypatch: pytest.MonkeyPatch) -> Rows:
    system = make_system()
    row = NotificationDelivery.objects.create(
        schedule=ReminderSchedule.objects.create(region=system.region, effective_from=NOW),
        scheduled_for=NOW,
        available_at=NOW,
        binding=system.binding,
        preparation_deadline=NOW,
        status="failed",
        last_error="mail_rejected",
        prepared_message=UNAVAILABLE,
    )
    return NotificationDelivery.objects.filter(pk=row.pk)


def invitation(system: System, email: str, **values: object) -> WorkspaceInvitation:
    return WorkspaceInvitation.objects.create(
        tenant=system.tenant,
        created_by=system.member,
        email=email,
        token_hash=uuid.uuid4().hex,
        expires_at=NOW + timedelta(days=7),
        **values,
    )


def open_invitation(monkeypatch: pytest.MonkeyPatch) -> Rows:
    return WorkspaceInvitation.objects.filter(pk=invitation(make_system(), "a@example.com").pk)


def accepted_invitation(monkeypatch: pytest.MonkeyPatch) -> Rows:
    accepted = invitation(make_system(), "a@example.com", accepted_at=NOW)
    return WorkspaceInvitation.objects.filter(pk=accepted.pk)


def acceptor_without_acceptance(monkeypatch: pytest.MonkeyPatch) -> Write:
    system = make_system()
    rows = WorkspaceInvitation.objects.filter(pk=invitation(system, "a@example.com").pk)
    return lambda: rows.update(accepted_by=system.member)


def second_open_invitation(**values: object) -> Stage:
    def stage(monkeypatch: pytest.MonkeyPatch) -> Write:
        system = make_system()
        invitation(system, "a@example.com", role="admin")
        rows = WorkspaceInvitation.objects.filter(pk=invitation(system, "b@example.com").pk)
        return lambda: rows.update(**values)

    return stage


def invitation_mail(monkeypatch: pytest.MonkeyPatch) -> Rows:
    system = make_system()
    mail = OutboundMail.objects.create(
        kind="workspace_invitation",
        recipient="guest@example.com",
        invitation=invitation(system, "guest@example.com"),
        language="en",
        created_at=NOW,
        available_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    return OutboundMail.objects.filter(pk=mail.pk)


def code_mail(monkeypatch: pytest.MonkeyPatch) -> Rows:
    mail = OutboundMail.objects.create(
        kind="account/email/login_code",
        recipient="person@example.com",
        language="en",
        sealed_code="sealed",
        created_at=NOW,
        available_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    return OutboundMail.objects.filter(pk=mail.pk)


def administrator_change(monkeypatch: pytest.MonkeyPatch) -> Write:
    system = make_system()
    change = WorkspaceAdminChange.objects.create(
        tenant=system.tenant, actor=system.admin, administrator=system.member
    )
    rows = WorkspaceAdminChange.objects.filter(pk=change.pk)
    return lambda: rows.update(previous_administrator=system.member)


def rejected_request(monkeypatch: pytest.MonkeyPatch) -> Rows:
    case = make_request_case(monkeypatch)
    request = production_request(case)
    reject_request(case.system.member, case.system.tenant.pk, request.pk, reason="Wrong meter")
    return ChangeRequest.objects.filter(pk=request.pk)


def incident_change(monkeypatch: pytest.MonkeyPatch) -> Rows:
    return IncidentChange.objects.filter(request=incident_request(make_request_case(monkeypatch)))


def reminder_schedule(monkeypatch: pytest.MonkeyPatch) -> Rows:
    system = make_system()
    schedule = ReminderSchedule.objects.create(region=system.region, effective_from=NOW)
    return ReminderSchedule.objects.filter(pk=schedule.pk)


def collection_schedule(monkeypatch: pytest.MonkeyPatch) -> Rows:
    system = make_system()
    schedule = CollectionSchedule.objects.create(
        region=system.region, times=["10:00"], effective_from=NOW
    )
    return CollectionSchedule.objects.filter(pk=schedule.pk)


def audit_event(monkeypatch: pytest.MonkeyPatch) -> Rows:
    event = AuditEvent.objects.filter(region=make_system().region).earliest("pk")
    return AuditEvent.objects.filter(pk=event.pk)


def second_slot(endpoint: str) -> Stage:
    def stage(monkeypatch: pytest.MonkeyPatch) -> Write:
        system = make_system()
        jobs = [
            CollectionJob.objects.create(
                binding=system.binding,
                endpoint=endpoint,
                scheduled_for=slot,
                target_date=NOW.date(),
                available_at=NOW,
            )
            for slot in (NOW, NOW + timedelta(hours=1))
        ]
        rows = CollectionJob.objects.filter(pk=jobs[1].pk)
        return lambda: rows.update(scheduled_for=NOW)

    return stage


def normalization(monkeypatch: pytest.MonkeyPatch) -> Rows:
    system, target = scaled_system(monkeypatch)
    actual_snapshot_for(system, date(2026, 11, 7))
    normalize(system, target)
    return ReferenceCapacityNormalization.objects.all()


def provider_account(monkeypatch: pytest.MonkeyPatch) -> Rows:
    return ProviderAccount.objects.filter(pk=make_system().account.pk)


def validated_binding(monkeypatch: pytest.MonkeyPatch) -> Rows:
    return SiteBinding.objects.filter(pk=make_system(approved=True).binding.pk)


def duplicate_binding_revision(monkeypatch: pytest.MonkeyPatch) -> Write:
    system = make_system()
    duplicate = SiteBinding.objects.get(pk=system.binding.pk)
    duplicate.pk = None
    duplicate.region_revision += 1
    duplicate.save()
    rows = SiteBinding.objects.filter(pk=duplicate.pk)
    return lambda: rows.update(region_revision=system.binding.region_revision)


def plant_automation(*, enabled: bool, by_member: bool, next_check: bool) -> Stage:
    def stage(monkeypatch: pytest.MonkeyPatch) -> Write:
        system, plant = make_plant(monkeypatch)
        rows = Plant.objects.filter(pk=plant.pk)
        return lambda: rows.update(
            automatic_calibration_enabled=enabled,
            automatic_calibration_enabled_by=system.member if by_member else None,
            next_calibration_check_at=NOW if next_check else None,
        )

    return stage


def observation(
    system: System, plant: Plant, start: datetime, length: timedelta, **values: object
) -> ProductionObservation:
    return ProductionObservation.objects.create(
        plant=plant,
        configuration=PlantConfiguration.objects.get(plant=plant),
        starts_at=start,
        ends_at=start + length,
        energy_mwh=1,
        source="manual",
        recorded_by=system.member,
        **values,
    )


def revision_of_another(*, start: timedelta, length: timedelta, plant: bool = False) -> Stage:
    def stage(monkeypatch: pytest.MonkeyPatch) -> Write:
        system, own = make_plant(monkeypatch)
        begins = NOW - timedelta(days=2)
        previous = observation(system, own, begins, timedelta(hours=1))
        ProductionObservation.objects.filter(pk=previous.pk).update(is_current=False)
        target = sibling_plant(system) if plant else own
        return lambda: observation(system, target, begins + start, length, supersedes=previous)

    return stage


def lease_breaches(
    table: str, rows: Callable[[pytest.MonkeyPatch], Rows], held: str, closed: str
) -> list[ParameterSet]:
    rule = (table, LEASE_RULES[table])
    guard = check(table, f"{table.split('_', 1)[1]}_lease_chk")
    branches: dict[str, dict[str, object]] = {
        "token-without-expiry": {"status": held, "lease_token": TOKEN},
        "expiry-without-token": {"lease_expires_at": NOW},
        f"{held}-without-lease": {"status": held},
        "lease-while-closed": {"status": closed, "lease_token": TOKEN, "lease_expires_at": NOW},
    }
    return [
        breach(name, rule, changing(rows, **values), guard) for name, values in branches.items()
    ]


TIMES = {
    "text": "10:00",
    "hour-out-of-range": ["24:00"],
    "single-digit-hour": ["9:00"],
    "seconds": ["10:00:00"],
    "trailing-newline": ["10:00\n"],
    "number": [600],
}
INVITATIONS = "accounts_workspaceinvitation"
MAIL = "notifications_outboundmail"
JOBS = "forecasts_collectionjob"
ACCOUNTS = "connections_provideraccount"
BINDINGS = "connections_sitebinding"
RUNS = "plants_calibrationrun"
PLANTS = "plants_plant"
OBSERVATIONS = "plants_productionobservation"
FACTOR_GUARD = check(
    "forecasts_referencecapacitynormalization",
    "referencecapacitynormalization_target_over_source_chk",
)
LINEAGE = row_trigger(
    OBSERVATIONS,
    "productionobservation_revision_guard",
    "A production revision supersedes the same plant and interval",
)
BREACHES = [
    *lease_breaches(JOBS, collection_job, "running", "pending"),
    *lease_breaches("metering_epiascollectionjob", epias_job, "running", "pending"),
    *lease_breaches(RUNS, calibration_run, "running", "pending"),
    *lease_breaches("notifications_notificationdelivery", delivery, "sending", "failed"),
    *lease_breaches(MAIL, invitation_mail, "sending", "pending"),
    breach(
        "previous-administrator-is-the-new-one",
        ("accounts_workspaceadminchange", ADMINISTRATOR_CHANGE_RULE),
        administrator_change,
        check("accounts_workspaceadminchange", "workspaceadminchange_administrator_chk"),
    ),
    breach(
        "reason-without-revocation",
        (INVITATIONS, INVITATION_STATE_RULE),
        changing(open_invitation, revoked_reason="cancelled"),
        check(INVITATIONS, "workspaceinvitation_revoked_at_chk"),
    ),
    breach(
        "revoked-after-acceptance",
        (INVITATIONS, INVITATION_STATE_RULE),
        changing(accepted_invitation, revoked_at=NOW, revoked_reason="cancelled"),
        check(INVITATIONS, "workspaceinvitation_revoked_at_chk"),
    ),
    breach(
        "second-open-for-one-address",
        (INVITATIONS, OPEN_INVITATION_RULE),
        second_open_invitation(email="a@example.com"),
        unique_index("workspaceinvitation_tenant_email_uniq"),
        rows=2,
    ),
    breach(
        "second-open-for-the-administrator",
        (INVITATIONS, OPEN_INVITATION_RULE),
        second_open_invitation(role="admin"),
        unique_index("workspaceinvitation_tenant_administrator_uniq"),
        rows=2,
    ),
    breach(
        "acceptor-without-acceptance",
        (INVITATIONS, INVITATION_ACCEPTOR_RULE),
        acceptor_without_acceptance,
        check(INVITATIONS, "workspaceinvitation_accepted_by_chk"),
    ),
    breach(
        "rejected-without-reason",
        ("change_requests_changerequest", REJECTION_REASON_RULE),
        changing(rejected_request, decision_reason=""),
        check("change_requests_changerequest", "changerequest_decision_reason_chk"),
    ),
    *(
        breach(
            name,
            ("change_requests_incidentchange", INCIDENT_INTERVAL_RULE),
            changing(incident_change, ends_at=F("starts_at") + shift),
            check("change_requests_incidentchange", "incidentchange_ends_at_chk"),
        )
        for name, shift in (
            ("ends-at-start", timedelta()),
            ("ends-before-start", -timedelta(hours=1)),
        )
    ),
    *(
        breach(
            name,
            (table, REMINDER_TIMES_RULE),
            changing(rows, times=times),
            check(table, f"{table.split('_', 1)[1]}_times_chk"),
        )
        for table, rows in (
            ("notifications_reminderschedule", reminder_schedule),
            ("forecasts_collectionschedule", collection_schedule),
        )
        for name, times in TIMES.items()
    ),
    breach(
        "code-of-a-mail-without-one",
        (MAIL, SEALED_CODE_RULE),
        changing(invitation_mail, sealed_code="sealed"),
        check(MAIL, "outboundmail_sealed_code_chk"),
    ),
    breach(
        "code-after-sending",
        (MAIL, SEALED_CODE_RULE),
        changing(code_mail, status="sent"),
        check(MAIL, "outboundmail_sealed_code_chk"),
    ),
    breach(
        "invitation-mail-without-invitation",
        (MAIL, MAIL_INVITATION_RULE),
        changing(invitation_mail, invitation=None),
        check(MAIL, "outboundmail_invitation_chk"),
    ),
    breach(
        "invitation-on-another-mail",
        (MAIL, MAIL_INVITATION_RULE),
        changing(invitation_mail, kind="plant_approved"),
        check(MAIL, "outboundmail_invitation_chk"),
    ),
    *(
        breach(
            name,
            (MAIL, MAIL_CONTEXT_RULE),
            changing(invitation_mail, context=context),
            check(MAIL, "outboundmail_context_chk"),
        )
        for name, context in (
            ("context-not-an-object", []),
            ("context-value-not-text", {"to_email": 1}),
            ("context-key-not-allowed", {"subject": "Welcome"}),
        )
    ),
    *(
        breach(
            name,
            ("regions_auditevent", AUDIT_DETAILS_RULE),
            changing(audit_event, details=details),
            check("regions_auditevent", "auditevent_details_chk"),
        )
        for name, details in (("details-a-list", []), ("details-text", "changed"))
    ),
    breach(
        "import-endpoint-without-import",
        (JOBS, IMPORT_SHAPE_RULE),
        changing(collection_job, endpoint="historical_import"),
        check(JOBS, "collectionjob_reference_import_chk"),
    ),
    breach(
        "import-under-another-endpoint",
        (JOBS, IMPORT_SHAPE_RULE),
        changing(reference_import_job, endpoint="scaled_forecasts"),
        check(JOBS, "collectionjob_reference_import_chk"),
    ),
    breach(
        "unfinished-import",
        (JOBS, IMPORT_SHAPE_RULE),
        changing(reference_import_job, status="pending"),
        check(JOBS, "collectionjob_import_status_chk"),
    ),
    *(
        breach(
            f"second-{endpoint}-job-of-a-slot",
            (JOBS, NATIVE_SLOT_RULE),
            second_slot(endpoint),
            unique_index("collectionjob_binding_endpoint_scheduled_for_uniq"),
            rows=2,
        )
        for endpoint in ("forecasts", "estimated_actuals")
    ),
    breach(
        "factor-not-the-quotient",
        ("forecasts_referencecapacitynormalization", NORMALIZATION_FACTOR_RULE),
        changing(normalization, factor=F("factor") * 2),
        FACTOR_GUARD,
    ),
    *(
        breach(
            name,
            (ACCOUNTS, PROVIDER_ACCOUNT_RULE),
            changing(provider_account, **values),
            check(ACCOUNTS, constraint),
        )
        for name, values, constraint in (
            ("no-quota", {"daily_quota": 0}, "provideraccount_daily_quota_chk"),
            ("quota-over-ten", {"daily_quota": 11}, "provideraccount_daily_quota_chk"),
            (
                "no-credential",
                {"credential_revision": 0},
                "provideraccount_credential_revision_chk",
            ),
            (
                "failure-of-no-credential",
                {"authentication_failed_revision": 0},
                "provideraccount_authentication_failed_revision_chk",
            ),
            (
                "failure-of-a-later-credential",
                {"authentication_failed_revision": F("credential_revision") + 1},
                "provideraccount_authentication_failed_revision_chk",
            ),
        )
    ),
    *(
        breach(
            name,
            (BINDINGS, SITE_BINDING_RULE),
            changing(validated_binding, **values),
            check(BINDINGS, "sitebinding_validated_chk"),
        )
        for name, values in (
            ("validated-without-revision", {"validated_credential_revision": None}),
            ("revision-without-moment", {"validated_at": None}),
            ("revision-of-no-credential", {"validated_credential_revision": 0}),
        )
    ),
    breach(
        "two-bindings-of-one-revision",
        (BINDINGS, BINDING_REVISION_RULE),
        duplicate_binding_revision,
        check(BINDINGS, "sitebinding_region_region_revision_uniq"),
        rows=2,
    ),
    breach(
        "finished-without-completion",
        (RUNS, CALIBRATION_RUN_RULE),
        changing(finished_run, completed_at=None),
        check(RUNS, "calibrationrun_completed_at_chk"),
    ),
    breach(
        "assessment-outside-the-vocabulary",
        (RUNS, CALIBRATION_RUN_RULE),
        changing(finished_run, assessment="excellent"),
        check(RUNS, "calibrationrun_assessment_chk"),
    ),
    *(
        breach(
            name,
            (PLANTS, PLANT_AUTOMATION_RULE),
            plant_automation(enabled=enabled, by_member=by_member, next_check=next_check),
            check(PLANTS, "plant_automation_chk"),
        )
        for name, enabled, by_member, next_check in (
            ("enabled-without-user", True, False, True),
            ("enabled-without-next-check", True, True, False),
            ("disabled-with-user", False, True, False),
            ("disabled-with-next-check", False, False, True),
        )
    ),
    breach(
        "revision-of-another-start",
        (OBSERVATIONS, OBSERVATION_LINEAGE_RULE),
        revision_of_another(start=timedelta(hours=1), length=timedelta(hours=1)),
        LINEAGE,
    ),
    breach(
        "revision-of-another-length",
        (OBSERVATIONS, OBSERVATION_LINEAGE_RULE),
        revision_of_another(start=timedelta(), length=timedelta(minutes=30)),
        LINEAGE,
    ),
    breach(
        "revision-of-another-plant",
        (OBSERVATIONS, OBSERVATION_LINEAGE_RULE),
        revision_of_another(start=timedelta(), length=timedelta(hours=1), plant=True),
        LINEAGE,
    ),
]


@contextmanager
def lifted(guard: Guard) -> Iterator[None]:
    with transaction.atomic():
        with connection.cursor() as cursor:
            # Deferred key checks the staged rows queued would block the DDL.
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute(guard.lift)
        yield
        transaction.set_rollback(True)


@pytest.mark.parametrize("case", BREACHES)
def test_the_database_refuses_a_breach_and_the_audit_reports_it_once_the_guard_is_lifted(
    case: Breach, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = case.stage(monkeypatch)
    assert stored_violations() == {}

    with pytest.raises(IntegrityError, match=case.guard.refusal), transaction.atomic():
        write()

    with lifted(case.guard):
        write()
        reported = stored_violations()

    table, rule = case.rule
    assert reported == {f"{table}: {rule}": case.rows}


def test_every_stored_rule_has_a_breach() -> None:
    covered = {param.values[0].rule for param in BREACHES if isinstance(param.values[0], Breach)}

    assert len(set(STORED_RULES)) == len(STORED_RULES)
    assert covered == set(STORED_RULES)


LARGEST = sys.float_info.max
SMALLEST = math.ulp(0.0)
# (source, target, factor) at the edges of float8: subnormal and overflowing quotients, and
# products or tolerances that leave float8 although the quotient is representable.
FLOAT_LIMITS = (
    (1e308, 1e-10, 1e-10 / 1e308),
    (1e308, 1e-10, math.nextafter(1e-10 / 1e308, 1)),
    (1e308, sys.float_info.min, SMALLEST),
    (4.0, SMALLEST, SMALLEST),
    (1.5, SMALLEST, SMALLEST / 1.5),
    (1.0, 1e-320, 1e-320),
    (3.0, LARGEST, LARGEST / 3),
    (0.5, 6e307, 6e307 / 0.5),
    (math.nextafter(1.0, 0), LARGEST, LARGEST),
    (0.5, LARGEST, 1.0),
    (1e-10, 1e300, 1.0),
    (2.0, 3.0, 1.5),
    (2.0, 3.0, math.nextafter(1.5, 2)),
)
QUOTIENTS = [factor == target / source for source, target, factor in FLOAT_LIMITS]
FACTOR_LABEL = f"forecasts_referencecapacitynormalization: {NORMALIZATION_FACTOR_RULE}"


def test_the_database_stores_a_factor_only_as_the_exact_quotient_at_the_float_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = normalization(monkeypatch)
    stored = []
    for source, target, factor in FLOAT_LIMITS:
        try:
            with transaction.atomic():
                rows.update(source_capacity_kw=source, target_capacity_kw=target, factor=factor)
        except IntegrityError, DataError:
            stored.append(False)
        else:
            stored.append(True)

    assert stored == QUOTIENTS


def test_the_audit_reports_exactly_the_factors_that_are_not_the_quotient_at_the_float_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = normalization(monkeypatch)
    reported = []
    with lifted(FACTOR_GUARD):
        for source, target, factor in FLOAT_LIMITS:
            rows.update(source_capacity_kw=source, target_capacity_kw=target, factor=factor)
            reported.append(stored_violations() == {FACTOR_LABEL: 1})

    assert reported == [not quotient for quotient in QUOTIENTS]
