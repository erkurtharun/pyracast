from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import models

from pyracast.irradiance import local_day_bounds
from pyracast.plants.events import record_plant_event
from pyracast.plants.models import Plant, PlantEventAction


class AutomationCause(models.TextChoices):
    TOGGLE = "toggle"
    MANUAL_ACTIVATION = "manual_activation"
    RESET = "reset"
    CONFIGURATION_REVISED = "configuration_revised"
    WORKSPACE_DISABLED = "workspace_disabled"


def next_check(plant: Plant, now: datetime) -> datetime:
    tomorrow = now.astimezone(ZoneInfo(plant.region.timezone)).date() + timedelta(days=1)
    return local_day_bounds(tomorrow, plant.region.timezone)[0]


def set_automation_state(
    plant: Plant, actor: User, *, enabled: bool, cause: AutomationCause, now: datetime
) -> None:
    """The only writer of the automation fields; the caller holds the plant lock."""
    if plant.automatic_calibration_enabled == enabled and (
        not enabled or plant.automatic_calibration_enabled_by_id == actor.pk
    ):
        return
    plant.automatic_calibration_enabled = enabled
    plant.automatic_calibration_enabled_by = actor if enabled else None
    plant.next_calibration_check_at = next_check(plant, now) if enabled else None
    plant.save(
        update_fields=(
            "automatic_calibration_enabled",
            "automatic_calibration_enabled_by",
            "next_calibration_check_at",
        )
    )
    record_plant_event(
        plant_id=plant.pk,
        actor=actor,
        action=PlantEventAction.CALIBRATION_AUTOMATION_ENABLED
        if enabled
        else PlantEventAction.CALIBRATION_AUTOMATION_DISABLED,
        details={"configuration_id": plant.current_configuration_id, "cause": cause},
    )
