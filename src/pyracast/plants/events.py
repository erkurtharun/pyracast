from collections.abc import Mapping

from django.contrib.auth.models import User

from pyracast.event_details import (
    INTEGER,
    INTEGERS,
    OPTIONAL_INTEGER,
    OPTIONAL_TEXT,
    TEXT,
    DetailShape,
    shaped,
)
from pyracast.plants.models import PlantEvent, PlantEventAction

EPIAS_CONNECTION: DetailShape = {"provider_plant_id": INTEGER, "eic": TEXT, "revision": INTEGER}
ACTIVATION: DetailShape = {
    "activation_id": INTEGER,
    "calibration_id": OPTIONAL_INTEGER,
    "configuration_id": INTEGER,
    "origin": TEXT,
    "enabled_by_id": OPTIONAL_INTEGER,
}
AUTOMATION: DetailShape = {"configuration_id": OPTIONAL_INTEGER}
PLANT_EVENT_DETAILS: Mapping[str, DetailShape] = {
    PlantEventAction.SUBMISSION_SAVED: {},
    PlantEventAction.CONFIGURATION_CREATED: {"configuration_id": INTEGER},
    PlantEventAction.PLANT_RENAMED: {"old": TEXT, "new": TEXT},
    PlantEventAction.OBSERVATIONS_IMPORTED: {
        "configuration_id": INTEGER,
        "created": INTEGER,
        "ids": INTEGERS,
    },
    PlantEventAction.PRODUCTION_CHANGED: {
        "origin": TEXT,
        "configuration_id": INTEGER,
        "created_ids": INTEGERS,
        "unchanged": INTEGER,
        "file_name": OPTIONAL_TEXT,
        "file_sha256": OPTIONAL_TEXT,
    },
    PlantEventAction.EPIAS_CONNECTED: EPIAS_CONNECTION,
    PlantEventAction.EPIAS_DISCONNECTED: EPIAS_CONNECTION,
    PlantEventAction.CALIBRATION_REQUESTED: {"calibration_id": INTEGER},
    PlantEventAction.CALIBRATION_SCHEDULED: {
        "calibration_id": INTEGER,
        "observation_id_cutoff": INTEGER,
    },
    PlantEventAction.CALIBRATION_COMPLETED: {
        "calibration_id": INTEGER,
        "status": TEXT,
        "dataset_sha256": TEXT,
    },
    PlantEventAction.CALIBRATION_ACTIVATED: ACTIVATION,
    PlantEventAction.CALIBRATION_AUTOMATICALLY_ACTIVATED: ACTIVATION,
    PlantEventAction.CALIBRATION_RESET: ACTIVATION,
    PlantEventAction.CALIBRATION_AUTOMATION_ENABLED: AUTOMATION,
    PlantEventAction.CALIBRATION_AUTOMATION_DISABLED: AUTOMATION,
    PlantEventAction.CALIBRATION_AUTOMATIC_SKIPPED: {
        "calibration_id": OPTIONAL_INTEGER,
        "configuration_id": OPTIONAL_INTEGER,
        "reason": TEXT,
        "origin": TEXT,
        "enabled_by_id": OPTIONAL_INTEGER,
    },
}


def record_plant_event(
    action: str,
    *,
    plant_id: int,
    actor: User | None,
    details: Mapping[str, object] | None = None,
) -> PlantEvent:
    return PlantEvent.objects.create(
        plant_id=plant_id,
        actor=actor,
        action=action,
        details=shaped("plant event", action, details or {}, PLANT_EVENT_DETAILS),
    )
