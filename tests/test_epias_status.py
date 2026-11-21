from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.plants.models import Plant
from tests.support.epias import PLANT
from tests.support.plant_api import setup_plant

pytestmark = pytest.mark.django_db(transaction=True)
LAST_NIGHT = datetime(2026, 11, 9, 21, tzinfo=UTC)
NIGHT_BEFORE = LAST_NIGHT - timedelta(days=1)


class Source:
    def __init__(self) -> None:
        self.client, base, plant = setup_plant()
        self.url = f"{base}{plant['id']}/production-source/"
        self.plant = Plant.objects.get(pk=plant["id"])
        self.connection: EpiasConnection | None = None

    def read(self) -> dict[str, Any]:
        response = self.client.get(self.url)
        assert response.status_code == 200, response.content
        payload: dict[str, Any] = response.json()
        return payload

    def connect(self) -> EpiasConnection:
        assert PLANT.eic is not None
        self.connection = EpiasConnection.objects.create(
            plant=self.plant,
            provider_plant_id=PLANT.id,
            eic=PLANT.eic,
            name=PLANT.name,
            connected_by_id=self.client.session["_auth_user_id"],
        )
        return self.connection

    def job(
        self, day: date, status: str, batch: datetime, *, complete: bool | None = None
    ) -> EpiasCollectionJob:
        connection, configuration = self.connection, self.plant.current_configuration
        assert connection is not None
        assert configuration is not None
        provider = {"id": connection.provider_plant_id, "eic": connection.eic}
        job = EpiasCollectionJob.objects.create(
            plant=self.plant,
            connection=connection,
            configuration=configuration,
            connection_revision=connection.revision,
            provider_plant=provider,
            target_date=day,
            scheduled_for=batch,
            available_at=batch,
            status=status,
        )
        if complete is not None:
            EpiasSnapshot.objects.create(
                job=job,
                attempt=1,
                connection=connection,
                configuration=job.configuration,
                provider_plant=provider,
                target_date=day,
                collected_at=batch,
                available_at=batch,
                readings=[{"total_mwh": "1"}],
                raw_payload={},
                content_sha256="a" * 64,
                complete=complete,
            )
        return job


def status(payload: dict[str, Any]) -> tuple[object, ...]:
    return (
        payload["state"],
        payload["health"],
        payload["complete_through"],
        payload["failed_days"],
        payload["disconnected_at"],
    )


def test_a_plant_without_a_production_source_was_never_connected() -> None:
    source = Source()

    assert status(source.read()) == ("never_connected", None, None, 0, None)


def test_a_connection_is_collecting_until_its_first_batch_ends_and_then_up_to_date() -> None:
    source = Source()
    source.connect()
    assert status(source.read()) == ("connected", "collecting", None, 0, None)

    source.job(date(2026, 11, 7), "succeeded", NIGHT_BEFORE, complete=True)
    source.job(date(2026, 11, 8), "succeeded", NIGHT_BEFORE, complete=False)
    newest = source.job(date(2026, 11, 9), "pending", LAST_NIGHT)
    assert status(source.read()) == ("connected", "collecting", "2026-11-07", 0, None)

    EpiasCollectionJob.objects.filter(pk=newest.pk).update(status="succeeded")
    assert status(source.read()) == ("connected", "up_to_date", "2026-11-07", 0, None)


def test_the_newest_batch_decides_between_waiting_and_attention() -> None:
    source = Source()
    source.connect()
    source.job(date(2026, 11, 6), "failed", NIGHT_BEFORE)
    waiting = source.job(date(2026, 11, 9), "awaiting_publication", LAST_NIGHT)
    source.job(date(2026, 11, 8), "succeeded", LAST_NIGHT, complete=True)
    assert status(source.read()) == ("connected", "waiting_for_publication", "2026-11-08", 0, None)

    EpiasCollectionJob.objects.filter(pk=waiting.pk).update(status="failed")
    source.job(date(2026, 11, 7), "failed", LAST_NIGHT)
    assert status(source.read()) == ("connected", "needs_attention", "2026-11-08", 2, None)


def test_a_disconnected_source_says_when_it_was_disconnected() -> None:
    source = Source()
    connection = source.connect()
    source.job(date(2026, 11, 8), "succeeded", LAST_NIGHT, complete=True)
    connection.enabled = False
    connection.save(update_fields=("enabled", "updated_at"))

    payload = source.read()

    assert status(payload)[:4] == ("disconnected", None, "2026-11-08", 0)
    assert datetime.fromisoformat(payload["disconnected_at"]) == connection.updated_at
