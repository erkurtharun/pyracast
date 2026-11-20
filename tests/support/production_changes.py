from datetime import datetime, timedelta

from pyracast.metering.models import EpiasConnection
from pyracast.plants.models import Plant
from pyracast.plants.production_changes import (
    ObservationImportResult,
    ProductionAction,
    ProductionChange,
    change_production,
)
from tests.support.domain import System


def write(
    system: System,
    plant: Plant,
    changes: list[ProductionChange],
    *,
    unit: str = "MWh",
    confirm_implausible: bool = False,
) -> ObservationImportResult:
    assert plant.current_configuration_id is not None
    return change_production(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit=unit,
        changes=changes,
        confirm_implausible=confirm_implausible,
    )


def edit(
    start: datetime,
    energy: float = 1,
    *,
    revision_id: int | None = None,
    action: ProductionAction = "set",
    minutes: int = 60,
    quality: str = "valid",
) -> ProductionChange:
    return ProductionChange(
        start, start + timedelta(minutes=minutes), revision_id, action, energy, quality, "meter"
    )


def provider(system: System, plant: Plant) -> EpiasConnection:
    return EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
