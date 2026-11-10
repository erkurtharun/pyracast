from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
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
    assert connected.json()["pending_jobs"] == 10
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
    assert values["calibration_blocker"] == "provisional_production"
    assert values["source_kind"] == "realtime_generation"
    assert values["available_sources"] == ["realtime_generation"]
    assert values["snapshot"]["id"] == snapshot.pk
    assert values["snapshot"]["configuration_id"] == plant["current_configuration"]["id"]
    assert [row["energy_mwh"] for row in values["readings"]] == [0, *([None] * 23)]
    assert [row["solar_mwh"] for row in values["readings"]] == [0, *([None] * 23)]
    assert [row["reported"] for row in values["readings"]] == [True, True, *([False] * 22)]
    assert values["readings"][2]["provider_timestamp"] is None
    first = values["readings"][0]
    assert datetime.fromisoformat(first["period_start"]) == stamp
    assert datetime.fromisoformat(first["period_end"]) == stamp + timedelta(hours=1)
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
    assert client.get(detail + "reported-production/", {"data_source": "bad"}).status_code == 400
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


def test_production_sources_remain_distinct_and_latest_settlement_keeps_missing_hours() -> None:
    client, path, connection = connected_source()
    provisional = archive_source(connection, "realtime_generation", {8: "20", 9: "22", 10: "24"})
    archive_source(connection, "settlement_injection", {8: "19", 9: "21", 10: "23"})
    revised = archive_source(connection, "settlement_injection", {8: "18", 10: "22"})
    result = client.get(path, {"date": "2020-01-01"})
    assert result.status_code == 200, result.content
    data = result.json()
    assert data["source_kind"] == "settlement_injection"
    assert data["snapshot"]["id"] == revised.pk
    assert set(data["available_sources"]) == {"realtime_generation", "settlement_injection"}
    assert data["calibration_eligible"] is True
    assert data["calibration_blocker"] is None
    assert [row["energy_mwh"] for row in data["readings"]][8:11] == [18, None, 22]
    assert data["readings"][9]["reported"] is False
    assert data["readings"][10]["period_start"] == "2020-01-01T07:00:00Z"
    assert data["readings"][10]["period_end"] == "2020-01-01T08:00:00Z"
    status = operations.get_epias_status(connection.plant_id, actor=connection.connected_by)
    assert status["first_settlement_date"] == date(2020, 1, 1)
    assert status["last_settlement_date"] == date(2020, 1, 1)
    old = client.get(path, {"date": "2020-01-01", "data_source": "realtime_generation"}).json()
    assert old["snapshot"]["id"] == provisional.pk
    assert old["calibration_eligible"] is False
    assert old["readings"][9]["energy_mwh"] == 22
    assert EpiasSnapshot.objects.count() == 3
    assert not ProductionObservation.objects.exists()


def test_unpublished_settlement_falls_back_only_in_automatic_view() -> None:
    client, path, connection = connected_source()
    realtime = archive_source(connection, "realtime_generation", {10: "10"})
    pending = archive_source(connection, "settlement_injection", {})
    auto = client.get(path, {"date": "2020-01-01"}).json()
    assert auto["snapshot"]["id"] == realtime.pk
    explicit = client.get(
        path, {"date": "2020-01-01", "data_source": "settlement_injection"}
    ).json()
    assert explicit["snapshot"]["id"] == pending.pk
    assert explicit["source_kind"] == "settlement_injection"
    assert explicit["readings"] == []
    assert explicit["calibration_blocker"] == "production_unavailable"
    with pytest.raises(ValidationError, match="invalid_production_source"):
        operations.read_epias_history(
            connection.plant_id, actor=connection.connected_by, source_kind="bad"
        )


@pytest.mark.parametrize(
    ("state", "blocker"),
    [
        ("disconnected", "source_disconnected"),
        ("ac", "measurement_basis_mismatch"),
        ("unconfigured", "configuration_mismatch"),
        ("suspended", "configuration_unavailable"),
    ],
)
def test_settlement_suitability_respects_configuration_and_connection(
    state: str, blocker: str
) -> None:
    client, path, connection = connected_source()
    archive_source(connection, "settlement_injection", {10: "10"})
    if state == "disconnected":
        connection.enabled = False
        connection.save()
    elif state == "ac":
        configuration = connection.plant.current_configuration
        assert configuration is not None
        configuration.measurement_basis = "ac_generation"
        configuration.save()
    elif state == "unconfigured":
        connection.plant.current_configuration = None
        connection.plant.save()
    else:
        region = connection.plant.region
        region.status = "suspended"
        region.save()
    response = client.get(path, {"date": "2020-01-01"})
    assert response.status_code == 200, response.content
    assert response.json()["calibration_eligible"] is False
    assert response.json()["calibration_blocker"] == blocker
