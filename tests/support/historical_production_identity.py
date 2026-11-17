from datetime import datetime

import pytest
from django.utils import timezone

from pyracast.metering.models import EpiasConnection
from pyracast.plants.models import PlantEvent


def record_identity(
    monkeypatch: pytest.MonkeyPatch, connection: EpiasConnection, stamp: datetime
) -> PlantEvent:
    monkeypatch.setattr(timezone, "now", lambda: stamp)
    connection.save()
    return PlantEvent.objects.create(
        plant=connection.plant,
        actor=connection.connected_by,
        action="epias_connected",
        details={
            "provider_plant_id": connection.provider_plant_id,
            "eic": connection.eic,
            "revision": connection.revision,
        },
    )
