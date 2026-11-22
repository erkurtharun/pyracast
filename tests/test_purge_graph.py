import ast
import inspect
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.apps import apps
from django.db import connection, models
from django.db.models import QuerySet

from pyracast import trash
from pyracast.notifications.scheduler import enqueue_due
from pyracast.plants.models import Plant
from pyracast.regions.models import Region
from pyracast.trash import RETENTION, trash_region
from tests.support.domain import NOW
from tests.support.notification_groups import group, member
from tests.support.notifications import SLOT
from tests.support.plant_operations import make_plant, snapshot_for
from tests.support.trash_operations import DETACHED, PURGED, revised_observation

ROOTS = frozenset(
    {
        "regions.Region",
        "plants.Plant",
        "connections.SiteBinding",
        "forecasts.CollectionJob",
        "forecasts.ForecastSnapshot",
        "plants.PlantConfiguration",
    }
)
# How the purge keeps every committed prefix consistent for each reference among purged rows.
ORDERED = "the referencing rows are deleted before the referenced table"
CLEARED = "the reference is cleared before the referenced table is deleted"
LEAVES_FIRST = "only rows no other row references are deleted, chunk by chunk"
FOREIGN_KEYS = {
    ("change_requests.ChangeRequest", "configuration"): ORDERED,
    ("change_requests.ChangeRequest", "plant"): ORDERED,
    ("change_requests.IncidentChange", "applied_revision"): ORDERED,
    ("change_requests.IncidentChange", "incident"): ORDERED,
    ("change_requests.IncidentChange", "request"): ORDERED,
    ("change_requests.ProductionChangeItem", "applied_observation"): ORDERED,
    ("change_requests.ProductionChangeItem", "expected_head"): ORDERED,
    ("change_requests.ProductionChangeItem", "request"): ORDERED,
    ("connections.SiteBinding", "region"): ORDERED,
    ("forecasts.CollectionJob", "binding"): ORDERED,
    ("forecasts.CollectionJob", "reference_import"): ORDERED,
    ("forecasts.CollectionSchedule", "region"): ORDERED,
    ("forecasts.ForecastResult", "snapshot"): ORDERED,
    ("forecasts.ForecastSnapshot", "binding"): ORDERED,
    ("forecasts.ForecastSnapshot", "job"): ORDERED,
    ("forecasts.HistoricalReferenceImport", "binding"): ORDERED,
    ("forecasts.ReferenceCapacityNormalization", "snapshot"): ORDERED,
    ("forecasts.ReferenceCapacityNormalization", "source_snapshot"): ORDERED,
    ("forecasts.ReferenceCapacityNormalization", "target_binding"): ORDERED,
    ("forecasts.RequestAttempt", "binding"): CLEARED,
    ("forecasts.RequestAttempt", "job"): CLEARED,
    ("incidents.IncidentRevision", "incident"): ORDERED,
    ("incidents.PlantIncident", "declared_configuration"): ORDERED,
    ("incidents.PlantIncident", "plant"): ORDERED,
    ("metering.EpiasCollectionJob", "configuration"): ORDERED,
    ("metering.EpiasCollectionJob", "connection"): ORDERED,
    ("metering.EpiasCollectionJob", "plant"): ORDERED,
    ("metering.EpiasConnection", "plant"): ORDERED,
    ("metering.EpiasConnectionRevision", "connection"): ORDERED,
    ("metering.EpiasSnapshot", "configuration"): ORDERED,
    ("metering.EpiasSnapshot", "connection"): ORDERED,
    ("metering.EpiasSnapshot", "job"): ORDERED,
    ("notifications.DeliveryRecipient", "delivery"): ORDERED,
    ("notifications.NotificationDelivery", "binding"): ORDERED,
    ("notifications.NotificationDelivery", "schedule"): ORDERED,
    ("notifications.NotificationDelivery", "snapshot"): ORDERED,
    ("notifications.ReminderRecipient", "schedule"): ORDERED,
    ("notifications.ReminderRecipientTie", "recipient"): ORDERED,
    ("notifications.ReminderSchedule", "region"): ORDERED,
    ("plants.CalibrationActivation", "calibration"): ORDERED,
    ("plants.CalibrationActivation", "configuration"): ORDERED,
    ("plants.CalibrationActivation", "plant"): ORDERED,
    ("plants.CalibrationRun", "binding"): ORDERED,
    ("plants.CalibrationRun", "comparison_activation"): CLEARED,
    ("plants.CalibrationRun", "configuration"): ORDERED,
    ("plants.CalibrationRun", "plant"): ORDERED,
    ("plants.ForecastIssuance", "calibration"): ORDERED,
    ("plants.ForecastIssuance", "configuration"): ORDERED,
    ("plants.ForecastIssuance", "plant"): ORDERED,
    ("plants.ForecastIssuance", "snapshot"): ORDERED,
    ("plants.Plant", "current_configuration"): CLEARED,
    ("plants.Plant", "region"): ORDERED,
    ("plants.PlantConfiguration", "binding"): ORDERED,
    ("plants.PlantConfiguration", "plant"): ORDERED,
    ("plants.PlantConfiguration", "region"): ORDERED,
    ("plants.PlantEvent", "plant"): ORDERED,
    ("plants.PlantSubmission", "base_configuration"): ORDERED,
    ("plants.PlantSubmission", "plant"): ORDERED,
    ("plants.ProductionObservation", "configuration"): ORDERED,
    ("plants.ProductionObservation", "plant"): ORDERED,
    ("plants.ProductionObservation", "supersedes"): LEAVES_FIRST,
    ("regions.AuditEvent", "region"): ORDERED,
    ("regions.Region", "approved_binding"): CLEARED,
    ("regions.RegionLocationChangeRequest", "region"): ORDERED,
}
DELETERS = frozenset({"_delete", "_delete_leaves_first"})


def _references(model: type[models.Model]) -> set[tuple[str, str]]:
    references: set[tuple[str, str]] = set()
    for field in model._meta.concrete_fields:
        if isinstance(field, models.ForeignKey) and field.remote_field.on_delete is not None:
            rule = field.remote_field.on_delete.__name__
            references.add((field.related_model._meta.label, rule))
    return references


def _dependants() -> set[str]:
    reached = set(ROOTS)
    while True:
        found = {
            model._meta.label
            for model in apps.get_models()
            if any(target in reached for target, _ in _references(model))
        } - reached
        if not found:
            return reached
        reached |= found


def _purged_references() -> dict[tuple[str, str], str]:
    """Every foreign key from a purged or detached row to a purged row, with its target."""
    return {
        (model._meta.label, field.name): field.related_model._meta.label
        for model in apps.get_models()
        if model._meta.label in PURGED | DETACHED
        for field in model._meta.concrete_fields
        if isinstance(field, models.ForeignKey) and field.related_model._meta.label in PURGED
    }


def _deleted_by_name() -> set[str]:
    names: set[str] = set()
    module = ast.parse(inspect.getsource(trash))
    callers = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name not in DELETERS
    ]
    for node in (inner for caller in callers for inner in ast.walk(caller)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in DELETERS:
                continue
            value = node.args[0]
            while isinstance(value, ast.Call | ast.Attribute):
                value = value.func if isinstance(value, ast.Call) else value.value
            if isinstance(value, ast.Name):
                names.add(value.id)
    labels = {model.__name__: model._meta.label for model in apps.get_models()}
    return {labels.get(name, name) for name in names}


def test_every_model_that_depends_on_a_purged_row_has_one_purge_rule() -> None:
    assert _dependants() == PURGED | DETACHED
    assert PURGED.isdisjoint(DETACHED)


def test_the_purge_deletes_exactly_the_models_named_for_it() -> None:
    assert _deleted_by_name() == PURGED


def test_every_reference_among_purged_rows_states_its_handling() -> None:
    references = _purged_references()
    assert set(references) == set(FOREIGN_KEYS)
    for (label, field), target in references.items():
        if label == target:
            assert FOREIGN_KEYS[label, field] != ORDERED, (label, field)


def _recording(
    events: list[tuple[str, str, frozenset[str]]], kind: str, original: Callable[..., None]
) -> Callable[..., None]:
    def record(queryset: QuerySet[Any], *args: Any, **values: Any) -> None:
        fields = values if kind == "detach" else args[:1] if kind == "leaves" else ()
        events.append((kind, queryset.model._meta.label, frozenset(fields)))
        original(queryset, *args, **values)

    return record


@pytest.mark.django_db(transaction=True)
def test_every_purge_statement_keeps_the_stated_references_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot_for(system, NOW.date() - timedelta(days=1))
    revised_observation(system, plant, 2)
    member(system, "holder@example.com")
    group(system, ["holder@example.com", "external@example.com"])
    assert enqueue_due(SLOT)
    trash_region(system.member, system.tenant.pk, system.region.pk)
    events: list[tuple[str, str, frozenset[str]]] = []
    for name, kind in (
        ("_delete", "delete"),
        ("_detach", "detach"),
        ("_delete_leaves_first", "leaves"),
    ):
        monkeypatch.setattr(trash, name, _recording(events, kind, getattr(trash, name)))
    original = trash._purge_region

    def statement_by_statement(region: Region, plants: list[Plant], budget: trash._Budget) -> None:
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        original(region, plants, budget)

    monkeypatch.setattr(trash, "_purge_region", statement_by_statement)
    monkeypatch.setattr(trash, "PURGE_CHUNK_SIZE", 1)

    assert trash.purge_expired(NOW + RETENTION) == {"regions": 1, "plants": 1}

    def positions(label: str, *kinds: str) -> list[int]:
        return [
            index
            for index, (kind, model, _) in enumerate(events)
            if model == label and kind in kinds
        ]

    for (label, field), target in _purged_references().items():
        removed = min(positions(target, "delete"))
        handling = FOREIGN_KEYS[label, field]
        if handling == ORDERED:
            assert max(positions(label, "delete")) < removed, (label, field)
        elif handling == CLEARED:
            cleared = [
                index
                for index, (kind, model, fields) in enumerate(events)
                if kind == "detach" and model == label and field in fields
            ]
            assert cleared, (label, field)
            assert cleared[0] < removed, (label, field)
        else:
            leaves = [event for event in events if event == ("leaves", label, frozenset({field}))]
            assert len(leaves) == len(positions(label, "delete")), (label, field)
