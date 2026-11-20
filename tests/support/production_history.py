from pyracast.plants.models import Plant
from pyracast.plants.production_changes import (
    ObservationImportResult,
    ObservationInput,
    import_observations,
)
from tests.support.domain import System


def upload(system: System, plant: Plant, rows: list[ObservationInput]) -> ObservationImportResult:
    assert plant.current_configuration_id is not None
    return import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="MWh",
        observations=rows,
    )
