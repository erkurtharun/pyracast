import ast
import inspect

from django.apps import apps
from django.db import models

from pyracast import trash

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
# Deleted by name in `pyracast.trash`, children before parents.
PURGED = frozenset(
    {
        "change_requests.ChangeRequest",
        "change_requests.IncidentChange",
        "change_requests.ProductionChangeItem",
        "connections.SiteBinding",
        "forecasts.CollectionJob",
        "forecasts.CollectionSchedule",
        "forecasts.ForecastResult",
        "forecasts.ForecastSnapshot",
        "forecasts.HistoricalReferenceImport",
        "forecasts.ReferenceCapacityNormalization",
        "incidents.IncidentRevision",
        "incidents.PlantIncident",
        "metering.EpiasCollectionJob",
        "metering.EpiasConnection",
        "metering.EpiasSnapshot",
        "notifications.NotificationDelivery",
        "notifications.ReminderSchedule",
        "plants.CalibrationActivation",
        "plants.CalibrationRun",
        "plants.ForecastIssuance",
        "plants.Plant",
        "plants.PlantConfiguration",
        "plants.PlantEvent",
        "plants.PlantSubmission",
        "plants.ProductionObservation",
        "regions.AuditEvent",
        "regions.Region",
        "regions.RegionLocationChangeRequest",
    }
)
# Kept for quota accounting; the purge clears their references to purged rows.
DETACHED = frozenset({"forecasts.RequestAttempt"})


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


def _deleted_by_name() -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(trash))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id != "_delete":
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
