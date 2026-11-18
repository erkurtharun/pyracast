from datetime import date, timedelta

from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.plants.models import PlantConfiguration


def settlement(
    connection: EpiasConnection,
    configuration: PlantConfiguration,
    day: date,
    *,
    value: int = 26,
    minute: int = 0,
    source_kind: str = "settlement_injection",
    identity: dict[str, object] | None = None,
) -> EpiasSnapshot:
    start, end = local_day_bounds(day, configuration.binding.timezone)
    stamp = end + timedelta(hours=2, minutes=minute)
    identity = identity or {"id": 77, "eic": "EIC", "name": "Plant"}
    job = EpiasCollectionJob.objects.create(
        plant_id=connection.plant_id,
        connection=connection,
        configuration=configuration,
        connection_revision=connection.revision,
        provider_plant=identity,
        source_kind=source_kind,
        target_day=day,
        scheduled_for=stamp,
        next_attempt_at=stamp,
        status="succeeded",
    )
    readings = [
        {
            "period_start": (start + timedelta(hours=h)).isoformat(),
            "period_end": (start + timedelta(hours=h + 1)).isoformat(),
            "total_mwh": str(value),
        }
        for h in range(24)
    ]
    return EpiasSnapshot.objects.create(
        job=job,
        attempt=1,
        connection=connection,
        configuration=configuration,
        target_day=day,
        source_kind=source_kind,
        provider_plant=identity,
        collected_at=stamp,
        available_at=stamp,
        raw_payload={},
        readings=readings,
        content_sha256=str(value) * 32,
        complete=True,
    )
