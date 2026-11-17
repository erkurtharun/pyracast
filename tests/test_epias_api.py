from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

import httpx
import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from pyracast.epias import AUTH_URL, EpiasClient, EpiasDay, EpiasReading
from pyracast.metering import operations
from pyracast.metering.jobs import run_epias_job
from pyracast.metering.models import EpiasCollectionJob, EpiasSnapshot
from pyracast.plants.models import Plant, ProductionObservation
from tests.support.api import verified_admin
from tests.support.epias import PASSWORD, PLANT, USERNAME
from tests.support.epias_api import archive_source, connected_source
from tests.support.plant_api import setup_plant

pytestmark = pytest.mark.django_db(transaction=True)


def test_production_source_reports_connection_and_archive_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, base, plant = setup_plant()
    _, operator = verified_admin()
    source = f"{base}{plant['id']}/production-source/"
    missing = client.get(source)
    assert missing.status_code == 200
    assert missing.json()["connected"] is False
    assert missing.json()["provider_plant_id"] is None

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text="TGT-test-api")
        return httpx.Response(
            200,
            json={
                "items": [{"id": PLANT.id, "eic": PLANT.eic, "name": PLANT.name, "shortName": "PV"}]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        provider = EpiasClient(transport, username=USERNAME, password=PASSWORD)
        monkeypatch.setattr(operations, "get_epias_client", lambda: provider)
        operations.connect_epias(
            plant["id"],
            actor=operator,
            provider_plant_id=PLANT.id,
            expected_eic=cast(str, PLANT.eic),
        )
    connected = client.get(source).json()
    assert connected["connected"] is True
    assert connected["pending_jobs"] == 102
    assert "meter_basis" not in connected
    assert EpiasCollectionJob.objects.filter(source_kind="realtime_generation").count() == 90
    assert EpiasCollectionJob.objects.filter(source_kind="settlement_injection").count() == 12
    job = EpiasCollectionJob.objects.order_by("-target_day").first()
    assert job is not None
    stamp = datetime.combine(job.target_day, time(), ZoneInfo("Europe/Istanbul"))
    data = EpiasDay(
        PLANT,
        job.target_day,
        (
            EpiasReading(stamp, "00:00", Decimal(0), Decimal(0)),
            EpiasReading(stamp + timedelta(hours=1), "01:00", None, None),
        ),
        timezone.now(),
        '{"items":[]}',
    )
    run_epias_job(job.pk, fetch=lambda *_: data)
    assert EpiasSnapshot.objects.filter(job=job).exists()
    assert not ProductionObservation.objects.exists()
    assert client.get(source).json()["archived_days"] == 1
    operations.disconnect_epias(plant["id"], actor=operator)
    assert client.get(source).json()["connected"] is False
    assert client.get(source).json()["archived_days"] == 1


def test_production_source_is_scoped_before_provider_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, base, plant_data = setup_plant()
    source = f"{base}{plant_data['id']}/production-source/"

    def forbidden_provider() -> EpiasClient:
        pytest.fail("Unauthorized requests must not use provider credentials.")

    monkeypatch.setattr(operations, "get_epias_client", forbidden_provider)
    outsider = User.objects.create_user("epias-outsider")
    foreign = Client()
    foreign.force_login(outsider)
    for viewer in (foreign, Client()):
        assert viewer.get(source).status_code == 403
    plant = Plant.objects.get(pk=plant_data["id"])
    plant.deleted_at = timezone.now()
    plant.save(update_fields=("deleted_at",))
    assert client.get(source).status_code == 404


def test_connection_status_reports_archived_settlement_dates() -> None:
    connection = connected_source()
    archive_source(connection, "realtime_generation", {8: "20"})
    archive_source(connection, "settlement_injection", {8: "19"})
    status = operations.get_epias_status(connection.plant_id, actor=connection.connected_by)
    assert status["first_settlement_date"] == date(2020, 1, 1)
    assert status["last_settlement_date"] == date(2020, 1, 1)
