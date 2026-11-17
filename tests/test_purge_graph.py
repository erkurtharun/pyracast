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
        "plants.ProductionObservation",
        "regions.AuditEvent",
        "regions.Region",
    }
)
CASCADED = frozenset(
    {
        "change_requests.IncidentChange",
        "change_requests.ProductionChangeItem",
        "plants.PlantSubmission",
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
        if isinstance(node, ast.Attribute) and node.attr == "delete":
            value = node.value
            while isinstance(value, ast.Call | ast.Attribute):
                value = value.func if isinstance(value, ast.Call) else value.value
            if isinstance(value, ast.Name):
                names.add(value.id)
    labels = {model.__name__: model._meta.label for model in apps.get_models()}
    return {labels.get(name, name) for name in names}


def test_every_model_that_depends_on_a_purged_row_has_one_purge_rule() -> None:
    rules = (PURGED, CASCADED, DETACHED)

    assert _dependants() == PURGED | CASCADED | DETACHED
    assert sum(len(rule) for rule in rules) == len(PURGED | CASCADED | DETACHED)


def test_the_purge_deletes_exactly_the_models_named_for_it() -> None:
    assert _deleted_by_name() == PURGED - {"plants.Plant", "regions.Region"} | {"plant", "region"}


def test_cascaded_models_reach_a_purged_parent_through_a_cascade() -> None:
    for label in CASCADED:
        cascades = {
            target for target, rule in _references(apps.get_model(label)) if rule == "CASCADE"
        }
        assert cascades & PURGED, label
