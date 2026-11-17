from datetime import datetime
from typing import Any

from pyracast.incidents.models import PlantIncident
from pyracast.incidents.operations import save_incident
from pyracast.plants.models import (
    Plant,
)
from tests.support.domain import System


def incident(
    system: System, plant: Plant, start: datetime, end: datetime | None, **caps: Any
) -> PlantIncident:
    return save_incident(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        kind="inverter",
        title="Inverter outage",
        notes="",
        starts_at=start,
        ends_at=end,
        **(
            {"available_dc_mw": 65, "available_inverter_mw": None, "available_export_mw": None}
            | caps
        ),
    )
