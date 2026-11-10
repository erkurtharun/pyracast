from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from pyracast.epias import AUTH_URL, EpiasClient, EpiasDay, EpiasError, EpiasReading
from pyracast.metering import operations
from pyracast.metering.jobs import run_epias_job
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.plants.models import Plant, ProductionObservation
from tests.test_epias import PASSWORD, PLANT, USERNAME
from tests.test_plant_api import setup_plant

pytestmark = pytest.mark.django_db(transaction=True)


def test_epias_api_connects_archives_native_values_and_preserves_history_after_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, base, plant = setup_plant()
    detail = f"{base}{plant['id']}/"
    source = detail + "production-source/"
    history = detail + "reported-production/"
    missing = client.get(source)
    assert missing.status_code == 200
    assert missing.json()["connected"] is False
    assert missing.json()["provider_plant_id"] is None
    assert client.get(history).json()["snapshot"] is None

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
        connected = client.put(
            source,
            {"provider_plant_id": PLANT.id, "expected_eic": PLANT.eic},
            content_type="application/json",
        )
    assert connected.status_code == 200, connected.content
    assert connected.json()["connected"] is True
    assert connected.json()["pending_jobs"] == 7
    assert connected.json()["meter_basis"] == "unknown"
    job = EpiasCollectionJob.objects.order_by("-target_day").first()
    assert job is not None
    stamp = datetime.combine(job.target_day, time(), ZoneInfo("Europe/Istanbul"))
    now = timezone.now()
    data = EpiasDay(
        PLANT,
        job.target_day,
        (
            EpiasReading(stamp, "00:00", Decimal(0), Decimal(0)),
            EpiasReading(stamp + timedelta(hours=1), "01:00", None, None),
        ),
        now,
        '{"items":[]}',
    )
    run_epias_job(job.pk, fetch=lambda *_: data)
    snapshot = EpiasSnapshot.objects.get(job=job)
    result = client.get(history, {"date": str(job.target_day)})
    assert result.status_code == 200, result.content
    values = result.json()
    assert values["unit"] == "MWh"
    assert values["calibration_eligible"] is False
    assert values["calibration_blocker"] == "measurement_contract_unverified"
    assert values["snapshot"]["id"] == snapshot.pk
    assert values["snapshot"]["configuration_id"] == plant["current_configuration"]["id"]
    assert [row["energy_mwh"] for row in values["readings"]] == [0, None]
    assert [row["solar_mwh"] for row in values["readings"]] == [0, None]
    assert "raw_payload" not in values["snapshot"]
    assert not ProductionObservation.objects.exists()
    assert client.get(source).json()["archived_days"] == 1
    assert client.delete(source).status_code == 204
    assert client.get(source).json()["connected"] is False
    assert (
        client.get(history, {"date": str(job.target_day)}).json()["snapshot"]["id"] == snapshot.pk
    )
    assert client.delete(source).status_code == 204


def test_epias_api_validates_inputs_and_returns_safe_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, base, plant = setup_plant()
    detail = f"{base}{plant['id']}/"
    source = detail + "production-source/"

    def unavailable() -> EpiasClient:
        raise EpiasError("credentials_unavailable")

    monkeypatch.setattr(operations, "get_epias_client", unavailable)
    assert client.put(source, {}, content_type="application/json").status_code == 400
    result = client.put(
        source,
        {"provider_plant_id": PLANT.id, "expected_eic": PLANT.eic},
        content_type="application/json",
    )
    assert result.status_code == 502
    assert result.json() == {"code": "epias_unavailable", "detail": "credentials_unavailable"}
    assert not EpiasConnection.objects.exists()
    assert client.get(detail + "reported-production/", {"date": "bad"}).status_code == 400
    today = timezone.now().astimezone(ZoneInfo("Europe/Istanbul")).date()
    assert client.get(detail + "reported-production/", {"date": str(today)}).status_code == 400
    assert client.delete(source).status_code == 204


def test_epias_api_scopes_every_action_before_provider_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, base, plant_data = setup_plant()
    detail = f"{base}{plant_data['id']}/"

    def forbidden_provider() -> EpiasClient:
        pytest.fail("Unauthorized requests must not use provider credentials.")

    monkeypatch.setattr(operations, "get_epias_client", forbidden_provider)
    outsider = User.objects.create_user("epias-outsider")
    foreign = Client()
    foreign.force_login(outsider)
    for viewer in (foreign, Client()):
        assert viewer.get(detail + "production-source/").status_code == 403
        assert viewer.get(detail + "reported-production/").status_code == 403
        assert viewer.delete(detail + "production-source/").status_code == 403
        assert (
            viewer.put(
                detail + "production-source/",
                {"provider_plant_id": PLANT.id, "expected_eic": PLANT.eic},
                content_type="application/json",
            ).status_code
            == 403
        )
    plant = Plant.objects.get(pk=plant_data["id"])
    plant.deleted_at = timezone.now()
    plant.save(update_fields=("deleted_at",))
    assert client.get(detail + "production-source/").status_code == 404
    assert client.get(detail + "reported-production/").status_code == 404
    assert client.delete(detail + "production-source/").status_code == 404
    assert (
        client.put(detail + "production-source/", {}, content_type="application/json").status_code
        == 404
    )
