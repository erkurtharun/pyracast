import ast
from pathlib import Path
from typing import Any

import pytest
from django.db import IntegrityError, transaction

from pyracast.accounts.audit import SECURITY_EVENT_DETAILS, SecurityEvent
from pyracast.db import violated_constraint
from pyracast.plants.events import PLANT_EVENT_DETAILS, record_plant_event
from pyracast.plants.models import PlantEvent, PlantEventAction
from pyracast.regions.audit import AUDIT_DETAILS, record_audit
from pyracast.regions.models import AuditAction, AuditEvent
from pyracast.regions.operations import (
    approve_location_change,
    reject_location_change,
    request_location_change,
)
from tests.support.domain import make_system
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)

SOURCE = Path(__file__).resolve().parents[1] / "src" / "pyracast"
ROW_WRITES = frozenset({"create", "bulk_create", "get_or_create", "update_or_create"})
TRAIL_WRITERS = {"AuditEvent": "regions/audit.py", "PlantEvent": "plants/events.py"}


def test_every_action_of_each_trail_declares_its_details() -> None:
    assert set(AUDIT_DETAILS) == set(AuditAction)
    assert set(PLANT_EVENT_DETAILS) == set(PlantEventAction)
    assert set(SECURITY_EVENT_DETAILS) == set(SecurityEvent)


def row_constructions(model: str) -> list[str]:
    found = []
    for path in SOURCE.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            built = isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id == model)
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in ROW_WRITES
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "objects"
                    and isinstance(node.func.value.value, ast.Name)
                    and node.func.value.value.id == model
                )
            )
            if built:
                found.append(path.relative_to(SOURCE).as_posix())
    return found


@pytest.mark.parametrize(("model", "writer"), TRAIL_WRITERS.items())
def test_trail_rows_are_written_only_by_their_trail_writer(model: str, writer: str) -> None:
    assert set(row_constructions(model)) == {writer}


@pytest.mark.parametrize(
    "details",
    [
        {},
        {"binding_id": 1, "note": "unexpected"},
        {"binding_id": True},
        {"binding_id": "1"},
    ],
    ids=["missing_key", "undeclared_key", "flag_for_identifier", "text_for_identifier"],
)
def test_an_audit_event_with_details_outside_its_shape_is_refused(details: dict[str, Any]) -> None:
    system = make_system()

    with pytest.raises(ValueError, match="region_approved"):
        record_audit(
            AuditAction.REGION_APPROVED,
            actor=system.admin,
            region=system.region,
            revision=system.region.revision,
            details=details,
        )
    assert not AuditEvent.objects.filter(action=AuditAction.REGION_APPROVED).exists()


def test_an_undeclared_audit_action_is_refused() -> None:
    system = make_system()
    stored = AuditEvent.objects.count()

    with pytest.raises(ValueError, match="region_archived"):
        record_audit(
            "region_archived", actor=system.admin, region=system.region, revision=1, details={}
        )
    assert AuditEvent.objects.count() == stored


@pytest.mark.parametrize(
    ("action", "details"),
    [
        ("plant_archived", {}),
        (PlantEventAction.CONFIGURATION_CREATED, {}),
        (PlantEventAction.CONFIGURATION_CREATED, {"configuration_id": 1, "reason": "manual"}),
        (PlantEventAction.PLANT_RENAMED, {"old": "Site", "new": None}),
    ],
    ids=["undeclared_action", "missing_key", "undeclared_key", "null_name"],
)
def test_a_plant_event_with_details_outside_its_shape_is_refused(
    action: str, details: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    system, plant = make_plant(monkeypatch)
    stored = PlantEvent.objects.count()

    with pytest.raises(ValueError, match=str(action)):
        record_plant_event(action, plant_id=plant.pk, actor=system.member, details=details)
    assert PlantEvent.objects.count() == stored


@pytest.mark.parametrize(
    "ids",
    [[1, "2"], [True], [None], [{"id": 1}], [[1]], [1.0], 1],
    ids=["text", "flag", "null", "object", "nested_list", "fraction", "not_a_list"],
)
def test_a_plant_event_with_identifiers_that_are_not_integers_is_refused(
    ids: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    system, plant = make_plant(monkeypatch)
    stored = PlantEvent.objects.count()

    with pytest.raises(ValueError, match="'production_changed' takes 'created_ids'"):
        record_plant_event(
            PlantEventAction.PRODUCTION_CHANGED,
            plant_id=plant.pk,
            actor=system.member,
            details={
                "origin": "editor",
                "configuration_id": 1,
                "created_ids": ids,
                "unchanged": 0,
                "file_name": None,
                "file_sha256": None,
            },
        )
    assert PlantEvent.objects.count() == stored


def test_binding_validation_and_approval_events_name_their_binding() -> None:
    system = make_system(approved=True)

    for action in (
        AuditAction.BINDING_CONFIGURED,
        AuditAction.CONNECTION_VALIDATED,
        AuditAction.REGION_APPROVED,
    ):
        assert AuditEvent.objects.get(region=system.region, action=action).details == {
            "binding_id": system.binding.pk
        }
    assert AuditEvent.objects.get(action=AuditAction.PROVIDER_ACCOUNT_CREATED).details == {}


def test_location_change_events_name_their_request() -> None:
    system = make_system()

    def requested(latitude: float) -> int:
        system.region.refresh_from_db()
        return request_location_change(
            system.member,
            system.region.pk,
            system.tenant.pk,
            expected_revision=system.region.revision,
            latitude=latitude,
            longitude=29,
            timezone="Europe/Istanbul",
        ).pk

    rejected = requested(41)
    reject_location_change(system.admin, rejected, expected_revision=1)
    approved = requested(42)
    approve_location_change(system.admin, approved, expected_revision=1)

    assert sorted(
        AuditEvent.objects.filter(action__startswith="location_change_").values_list(
            "action", "details"
        ),
        key=str,
    ) == sorted(
        [
            (AuditAction.LOCATION_CHANGE_REQUESTED, {"request_id": rejected}),
            (AuditAction.LOCATION_CHANGE_REJECTED, {"request_id": rejected}),
            (AuditAction.LOCATION_CHANGE_REQUESTED, {"request_id": approved}),
            (AuditAction.LOCATION_CHANGE_APPROVED, {"request_id": approved}),
        ],
        key=str,
    )


@pytest.mark.parametrize("details", [[], "binding", 1], ids=["list", "text", "number"])
def test_the_database_refuses_audit_details_that_are_not_an_object(details: object) -> None:
    make_system()
    rows = AuditEvent.objects.filter(action=AuditAction.BINDING_CONFIGURED)

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        rows.update(details=details)
    assert violated_constraint(raised.value) == "auditevent_details_chk"
