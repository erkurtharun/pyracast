from datetime import date, timedelta

import pytest

from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.plants.models import PlantConfiguration
from tests.support.plant_operations import make_plant

DAY = date(2026, 11, 1)


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch) -> tuple[PlantConfiguration, EpiasConnection]:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    return configuration, connection


def acquisition(
    source: tuple[PlantConfiguration, EpiasConnection],
    kind: str = "realtime_generation",
    *,
    minute: int = 0,
    values: list[str | None] | None = None,
) -> EpiasSnapshot:
    configuration, connection = source
    start, end = local_day_bounds(DAY, "Europe/Istanbul")
    stamp = end + timedelta(hours=2, minutes=minute)
    identity = {
        "id": 77 if kind == "settlement_injection" else 2,
        "eic": "EIC",
        "name": "Plant",
    }
    job = EpiasCollectionJob.objects.create(
        plant_id=connection.plant_id,
        connection=connection,
        configuration=configuration,
        connection_revision=connection.revision,
        provider_plant=identity,
        source_kind=kind,
        target_day=DAY,
        scheduled_for=stamp,
        next_attempt_at=stamp,
        status="succeeded",
    )
    readings = [
        {
            "period_start": (start + timedelta(hours=hour)).isoformat(),
            "period_end": (start + timedelta(hours=hour + 1)).isoformat(),
            "total_mwh": value,
        }
        for hour, value in enumerate(values if values is not None else ["26"] * 24)
    ]
    return EpiasSnapshot.objects.create(
        job=job,
        attempt=1,
        connection=connection,
        configuration=configuration,
        target_day=DAY,
        source_kind=kind,
        provider_plant=identity,
        collected_at=stamp,
        available_at=stamp,
        readings=readings,
        raw_payload={},
        content_sha256="a" * 64,
    )
