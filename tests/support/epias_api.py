from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.test import Client
from django.utils import timezone

from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.plants.models import Plant
from tests.support.epias import PLANT
from tests.support.plant_api import setup_plant


def archive_source(
    connection: EpiasConnection,
    source_kind: str,
    values: dict[int, str | None],
) -> EpiasSnapshot:
    now = timezone.now()
    day = date(2020, 1, 1)
    configuration = connection.plant.current_configuration
    assert configuration is not None
    provider = {
        "id": connection.settlement_plant_id
        if source_kind == "settlement_injection"
        else connection.provider_plant_id,
        "eic": connection.eic,
    }
    job = EpiasCollectionJob.objects.create(
        plant_id=connection.plant_id,
        connection=connection,
        configuration=configuration,
        connection_revision=connection.revision,
        provider_plant=provider,
        target_day=day,
        scheduled_for=now,
        next_attempt_at=now,
        source_kind=source_kind,
        status="succeeded",
    )
    midnight = datetime.combine(day, time(), ZoneInfo("Europe/Istanbul"))
    readings = [
        {
            "provider_timestamp": (midnight + timedelta(hours=hour)).isoformat(),
            "period_start": (midnight + timedelta(hours=hour)).isoformat(),
            "period_end": (midnight + timedelta(hours=hour + 1)).isoformat(),
            "total_mwh": energy,
            "sun_mwh": energy,
        }
        for hour, energy in values.items()
    ]
    return EpiasSnapshot.objects.create(
        job=job,
        attempt=1,
        connection=connection,
        configuration=configuration,
        provider_plant=provider,
        target_day=day,
        source_kind=source_kind,
        collected_at=now,
        available_at=now,
        readings=readings,
        raw_payload={"items": readings},
        content_sha256="a" * 64,
    )


def connected_source() -> tuple[Client, str, EpiasConnection]:
    client, base, plant_data = setup_plant()
    plant = Plant.objects.get(pk=plant_data["id"])
    assert PLANT.eic is not None
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=PLANT.id,
        settlement_plant_id=789,
        eic=PLANT.eic,
        name=PLANT.name,
        connected_by_id=client.session["_auth_user_id"],
    )
    return client, f"{base}{plant.pk}/reported-production/", connection
