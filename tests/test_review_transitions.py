import itertools
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.db.models import Field

from pyracast.errors import ConflictError
from pyracast.forecasts.validation import validate_binding
from pyracast.plants.approval import (
    DECLARED_FIELDS,
    UNDECLARED_FIELDS,
    ReviewDecision,
    ReviewSnapshot,
    approve_region,
    decide_review,
    declaration_fingerprint,
    review_snapshot,
)
from pyracast.plants.models import PlantConfiguration, PlantSubmission
from pyracast.regions.models import AuditAction, AuditEvent, Region, RegionStatus
from pyracast.regions.review import TRANSITIONS, set_review_status
from tests.support.admin_setup import submitted_plant
from tests.support.domain import NOW, FakeAdapter, make_system

pytestmark = pytest.mark.django_db(transaction=True)

S = RegionStatus
REASON = "The declared capacity does not match the grid licence."
ALLOWED = {
    (S.PENDING, S.APPROVED),
    (S.PENDING, S.REJECTED),
    (S.APPROVED, S.SUSPENDED),
    (S.SUSPENDED, S.APPROVED),
    (S.APPROVED, S.PENDING),
    (S.SUSPENDED, S.PENDING),
    (S.REJECTED, S.PENDING),
}
KEEP_BINDING = {S.APPROVED, S.SUSPENDED}
CHANGED_VALUES: dict[str, object] = {
    "dc_capacity_mw": 4.3,
    "inverter_capacity_mw": 3.9,
    "export_limit_mw": 3.7,
    "measurement_basis": "ac_generation",
    "mounting_type": "fixed",
    "surface_tilt_degrees": 20.0,
    "surface_azimuth_degrees": 180.0,
}


def test_the_transition_table_holds_exactly_the_review_rules() -> None:
    assert set(TRANSITIONS) == ALLOWED


@pytest.mark.parametrize(("source", "target"), list(itertools.product(S, S)))
def test_a_review_status_moves_only_along_the_transition_table(
    source: RegionStatus, target: RegionStatus
) -> None:
    system = make_system()
    Region.objects.filter(pk=system.region.pk).update(
        status=source, approved_binding=system.binding if source in KEEP_BINDING else None
    )
    region = Region.objects.get(pk=system.region.pk)

    if (source, target) in ALLOWED:
        set_review_status(region, target, binding=system.binding)
        region.refresh_from_db()
        assert region.status == target
        assert (region.approved_binding_id is not None) == (target in KEEP_BINDING)
    else:
        with pytest.raises(ConflictError):
            set_review_status(region, target, binding=system.binding)
        assert Region.objects.get(pk=region.pk).status == source


def test_every_submission_field_is_classified_for_the_declaration() -> None:
    fields = {
        field.name for field in PlantSubmission._meta.concrete_fields if isinstance(field, Field)
    }

    assert fields == DECLARED_FIELDS | UNDECLARED_FIELDS
    assert not DECLARED_FIELDS & UNDECLARED_FIELDS


@pytest.mark.parametrize("field", sorted(CHANGED_VALUES))
def test_a_change_of_any_declared_field_changes_the_declaration(field: str) -> None:
    system = make_system()
    plant = submitted_plant(system)
    before = declaration_fingerprint(system.region.pk)
    changes = {field: CHANGED_VALUES[field]}
    if field.startswith("surface_"):
        changes["mounting_type"] = "fixed"

    PlantSubmission.objects.filter(plant=plant).update(**changes)

    assert field in DECLARED_FIELDS
    assert declaration_fingerprint(system.region.pk) != before


def test_a_decision_to_the_current_status_writes_no_second_audit_row() -> None:
    system = make_system()
    seen = review_snapshot(system.region.pk)

    first = decide_review(
        system.admin, system.region.pk, ReviewDecision.REJECT, reason=REASON, seen=seen
    )
    second = decide_review(
        system.admin, system.region.pk, ReviewDecision.REJECT, reason=REASON, seen=seen
    )

    assert (first, second) == (True, False)
    assert AuditEvent.objects.filter(action=AuditAction.REGION_REJECTED).count() == 1


def test_a_repeated_approval_reports_that_nothing_changed() -> None:
    system = make_system()
    submitted_plant(system)
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)

    first = approve_region(system.admin, system.region.pk, system.binding.pk, NOW)
    second = approve_region(system.admin, system.region.pk, system.binding.pk, NOW)

    assert (first.changed, second.changed) == (True, False)
    assert first.job.pk == second.job.pk


def test_a_rejected_plant_is_not_approved_by_a_program_caller() -> None:
    system = make_system()
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    decide_review(system.admin, system.region.pk, ReviewDecision.REJECT, reason=REASON)

    with pytest.raises(ConflictError):
        approve_region(system.admin, system.region.pk, system.binding.pk, NOW)

    assert Region.objects.get(pk=system.region.pk).status == S.REJECTED


def test_suspending_a_plant_that_is_not_approved_is_refused() -> None:
    system = make_system()
    seen = review_snapshot(system.region.pk)

    with pytest.raises(ConflictError):
        decide_review(
            system.admin, system.region.pk, ReviewDecision.SUSPEND, reason=REASON, seen=seen
        )

    assert Region.objects.get(pk=system.region.pk).status == S.PENDING
    assert not AuditEvent.objects.filter(action=AuditAction.REGION_SUSPENDED).exists()


def test_an_edit_committed_while_approval_waits_for_the_plant_lock_refuses_the_approval() -> None:
    system = make_system()
    plant = submitted_plant(system)
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    seen = review_snapshot(system.region.pk)
    approver_pid: list[int] = []

    def approve() -> object:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                approver_pid.extend(cursor.fetchone() or ())
            return approve_region(system.admin, system.region.pk, system.binding.pk, NOW, seen=seen)
        except ConflictError as error:
            return error
        finally:
            connections.close_all()

    pool = ThreadPoolExecutor(1)
    try:
        with transaction.atomic():
            Region.objects.select_for_update().get(pk=system.region.pk)
            PlantSubmission.objects.filter(plant=plant).update(dc_capacity_mw=8.8)
            outcome = pool.submit(approve)
            for _ in range(1000):
                if approver_pid and _blocked(approver_pid[0]):
                    break
                time.sleep(0.01)
            assert approver_pid
            assert _blocked(approver_pid[0])
        result = outcome.result(timeout=10)
    finally:
        pool.shutdown()

    assert isinstance(result, ConflictError)
    assert Region.objects.get(pk=system.region.pk).status == S.PENDING
    assert not PlantConfiguration.objects.filter(plant=plant).exists()


def _blocked(pid: int) -> bool:
    with connections["default"].cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE pid = %s AND wait_event_type = 'Lock'",
            [pid],
        )
        row = cursor.fetchone()
    return bool(row and row[0])


def test_a_snapshot_names_the_status_and_declaration_it_saw() -> None:
    system = make_system()
    submitted_plant(system)

    assert review_snapshot(system.region.pk) == ReviewSnapshot(
        status=S.PENDING,
        revision=system.region.revision,
        declaration=declaration_fingerprint(system.region.pk),
    )


def test_a_decision_from_a_page_of_an_earlier_revision_is_refused() -> None:
    system = make_system()
    seen = review_snapshot(system.region.pk)
    Region.objects.filter(pk=system.region.pk).update(revision=seen.revision + 1)

    with pytest.raises(ConflictError):
        decide_review(
            system.admin, system.region.pk, ReviewDecision.REJECT, reason=REASON, seen=seen
        )

    assert Region.objects.get(pk=system.region.pk).status == S.PENDING


def test_an_unknown_review_decision_is_refused() -> None:
    system = make_system()

    with pytest.raises(ValidationError, match=r"Choose a review decision\."):
        decide_review(system.admin, system.region.pk, "approve", reason=REASON)
