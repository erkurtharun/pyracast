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
from pyracast.regions.models import Region
from tests.support.api import verified_admin
from tests.support.epias import PASSWORD, PLANT, USERNAME
from tests.support.epias_api import archive_source, connected_source
from tests.support.plant_api import setup_plant

pytestmark = pytest.mark.django_db(transaction=True)


def test_epias_api_connects_archives_native_values_and_preserves_history_after_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, base, plant = setup_plant()
    admin, _ = verified_admin()
    detail = f"{base}{plant['id']}/"
    source = detail + "production-source/"
    history = detail + "reported-production/"
    missing = client.get(source)
    assert missing.status_code == 200
    assert missing.json()["connected"] is False
    assert missing.json()["provider_plant_id"] is None
    assert client.get(history).json()["snapshot"] is None
    absent = client.get(history, {"data_source": "settlement_injection"}).json()
    assert absent["snapshot"] is None
    assert absent["readings"] == []
    assert absent["complete"] is False

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
        connected = admin.put(
            source,
            {"provider_plant_id": PLANT.id, "expected_eic": PLANT.eic},
            content_type="application/json",
        )
    assert connected.status_code == 200, connected.content
    assert connected.json()["connected"] is True
    assert connected.json()["pending_jobs"] == 102
    assert EpiasCollectionJob.objects.filter(source_kind="realtime_generation").count() == 90
    assert EpiasCollectionJob.objects.filter(source_kind="settlement_injection").count() == 12
    assert "meter_basis" not in connected.json()
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
    assert values["calibration_eligible"] is True
    assert values["calibration_blocker"] is None
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
    assert admin.delete(source).status_code == 204
    assert client.get(source).json()["connected"] is False
    assert (
        client.get(history, {"date": str(job.target_day)}).json()["snapshot"]["id"] == snapshot.pk
    )
    assert admin.delete(source).status_code == 204


@pytest.mark.parametrize(
    "error_detail",
    [
        "credentials_unavailable",
        "Provider failure: synthetic-private-ticket at /private/provider.py:42",
    ],
)
def test_epias_api_validates_inputs_and_returns_safe_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_detail: str,
) -> None:
    client, base, plant = setup_plant()
    admin, _ = verified_admin()
    detail = f"{base}{plant['id']}/"
    source = detail + "production-source/"

    def unavailable() -> EpiasClient:
        raise EpiasError(error_detail)

    monkeypatch.setattr(operations, "get_epias_client", unavailable)
    assert admin.put(source, {}, content_type="application/json").status_code == 400
    result = admin.put(
        source,
        {"provider_plant_id": PLANT.id, "expected_eic": PLANT.eic},
        content_type="application/json",
    )
    assert result.status_code == 502
    assert result.json() == {
        "code": "epias_unavailable",
        "detail": "EPIAS connection is currently unavailable. Please try again later.",
    }
    assert error_detail not in caplog.text
    assert not EpiasConnection.objects.exists()
    assert client.get(detail + "reported-production/", {"date": "bad"}).status_code == 400
    assert client.get(detail + "reported-production/", {"data_source": "bad"}).status_code == 400
    today = timezone.now().astimezone(ZoneInfo("Europe/Istanbul")).date()
    assert client.get(detail + "reported-production/", {"date": str(today)}).status_code == 400
    assert admin.delete(source).status_code == 204


def test_epias_api_scopes_every_action_before_provider_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, base, plant_data = setup_plant()
    admin, _ = verified_admin()
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
    Region.objects.filter(pk=plant.region_id).update(deleted_at=timezone.now())
    assert client.get(detail + "production-source/").status_code == 404
    assert client.get(detail + "reported-production/").status_code == 404
    assert admin.delete(detail + "production-source/").status_code == 404
    assert (
        admin.put(detail + "production-source/", {}, content_type="application/json").status_code
        == 404
    )


def test_automatic_production_merges_latest_sources_and_explicit_views_keep_missing_hours() -> None:
    client, path, connection = connected_source()
    provisional = archive_source(connection, "realtime_generation", {8: "20", 9: "22", 10: "24"})
    archive_source(connection, "settlement_injection", {8: "19", 9: "21", 10: "23"})
    revised = archive_source(connection, "settlement_injection", {8: "18", 10: "22"})
    result = client.get(path, {"date": "2020-01-01"})
    assert result.status_code == 200, result.content
    data = result.json()
    assert data["source_kind"] == "mixed"
    assert data["complete"] is False
    assert [snapshot["id"] for snapshot in data["source_snapshots"]] == [revised.pk, provisional.pk]
    assert data["snapshot"]["id"] == revised.pk
    assert set(data["available_sources"]) == {"realtime_generation", "settlement_injection"}
    assert data["calibration_eligible"] is True
    assert data["calibration_blocker"] is None
    assert [row["energy_mwh"] for row in data["readings"]][8:11] == [18, 22, 22]
    assert data["readings"][9]["reported"] is True
    assert data["readings"][8]["snapshot_id"] == revised.pk
    assert data["readings"][8]["source_kind"] == "settlement_injection"
    assert data["readings"][9]["snapshot_id"] == provisional.pk
    assert data["readings"][9]["source_kind"] == "realtime_generation"
    assert data["readings"][7]["energy_mwh"] is None
    assert data["readings"][7]["snapshot_id"] is None
    assert data["readings"][7]["source_kind"] is None
    assert data["readings"][10]["period_start"] == "2020-01-01T07:00:00Z"
    assert data["readings"][10]["period_end"] == "2020-01-01T08:00:00Z"
    status = operations.get_epias_status(connection.plant_id, actor=connection.connected_by)
    assert status["first_settlement_date"] == date(2020, 1, 1)
    assert status["last_settlement_date"] == date(2020, 1, 1)
    old = client.get(path, {"date": "2020-01-01", "data_source": "realtime_generation"}).json()
    assert old["snapshot"]["id"] == provisional.pk
    assert old["calibration_eligible"] is True
    assert old["readings"][9]["energy_mwh"] == 22
    explicit = client.get(
        path, {"date": "2020-01-01", "data_source": "settlement_injection"}
    ).json()
    assert explicit["readings"][9]["energy_mwh"] is None
    assert explicit["readings"][9]["reported"] is False
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
    assert explicit["complete"] is False
    assert explicit["source_snapshots"] == []
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
@pytest.mark.parametrize(
    "sources",
    [
        ("settlement_injection",),
        ("realtime_generation",),
        ("realtime_generation", "settlement_injection"),
    ],
)
def test_settlement_suitability_respects_configuration_and_connection(
    state: str, blocker: str, sources: tuple[str, ...]
) -> None:
    client, path, connection = connected_source()
    for hour, source in enumerate(sources, start=10):
        archive_source(connection, source, {hour: "10"})
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


def test_new_settlement_replaces_provisional_hours_without_rewriting_archive() -> None:
    client, path, connection = connected_source()
    provisional = archive_source(connection, "realtime_generation", dict.fromkeys(range(24), "5"))
    original = provisional.readings
    before = client.get(path, {"date": "2020-01-01"}).json()
    assert before["source_kind"] == "realtime_generation"
    assert before["complete"] is True
    assert before["reported_total_mwh"] == 120
    assert before["calibration_eligible"] is True
    assert [snapshot["id"] for snapshot in before["source_snapshots"]] == [provisional.pk]
    final = archive_source(connection, "settlement_injection", {8: "0", 9: "7"})
    after = client.get(path, {"date": "2020-01-01"}).json()
    assert after["source_kind"] == "mixed"
    assert after["complete"] is True
    assert after["reported_total_mwh"] == 117
    assert after["snapshot"]["id"] == final.pk
    assert after["readings"][8]["energy_mwh"] == 0
    assert after["readings"][8]["snapshot_id"] == final.pk
    assert after["readings"][9]["energy_mwh"] == 7
    assert after["readings"][10]["energy_mwh"] == 5
    provisional.refresh_from_db()
    assert provisional.readings == original
    assert provisional.raw_payload == {"items": original}
    assert EpiasSnapshot.objects.count() == 2
    assert not ProductionObservation.objects.exists()


def test_latest_missing_values_never_fill_from_older_source_acquisitions() -> None:
    client, path, connection = connected_source()
    archive_source(connection, "realtime_generation", {8: "99", 9: "99"})
    realtime = archive_source(connection, "realtime_generation", {8: "4"})
    archive_source(connection, "settlement_injection", {8: "88", 9: "88"})
    final = archive_source(connection, "settlement_injection", {8: None, 9: None, 10: "0"})
    data = client.get(path, {"date": "2020-01-01"}).json()
    assert data["source_kind"] == "mixed"
    assert data["complete"] is False
    assert [row["energy_mwh"] for row in data["readings"]][8:11] == [4, None, 0]
    assert data["readings"][8]["snapshot_id"] == realtime.pk
    assert data["readings"][9]["snapshot_id"] is None
    assert data["readings"][10]["snapshot_id"] == final.pk


def test_unused_provisional_snapshot_does_not_label_final_day_mixed() -> None:
    client, path, connection = connected_source()
    archive_source(connection, "realtime_generation", {10: "10"})
    final = archive_source(connection, "settlement_injection", {10: "0"})
    data = client.get(path, {"date": "2020-01-01"}).json()
    assert data["source_kind"] == "settlement_injection"
    assert [snapshot["id"] for snapshot in data["source_snapshots"]] == [final.pk]
    assert data["readings"][10]["energy_mwh"] == 0


def test_complementary_partial_sources_form_complete_day() -> None:
    client, path, connection = connected_source()
    realtime = archive_source(
        connection, "realtime_generation", dict.fromkeys(range(0, 24, 2), "0")
    )
    final = archive_source(connection, "settlement_injection", dict.fromkeys(range(1, 24, 2), "0"))
    data = client.get(path, {"date": "2020-01-01"}).json()
    assert data["source_kind"] == "mixed"
    assert data["complete"] is True
    assert data["reported_total_mwh"] == 0
    assert [snapshot["complete"] for snapshot in data["source_snapshots"]] == [False, False]
    assert [row["energy_mwh"] for row in data["readings"]] == [0] * 24
    assert [row["snapshot_id"] for row in data["readings"]] == [realtime.pk, final.pk] * 12


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (dict.fromkeys(range(19), "10"), 190),
        (dict.fromkeys(range(8, 18), "10"), 100),
        (dict.fromkeys(range(8, 18), "0"), 0),
        (dict.fromkeys(range(9, 18), "10"), None),
        (dict.fromkeys(range(8, 17), "10"), None),
        (dict.fromkeys(range(8, 18), "10") | {12: None}, None),
        (dict.fromkeys(range(8, 18), "10") | {12: "-1"}, None),
        (dict.fromkeys(range(8, 18), "10") | {22: "3"}, 103),
        (dict.fromkeys(range(8, 18), "1e308"), None),
        ({}, None),
    ],
)
@pytest.mark.parametrize("source", ["auto", "realtime_generation"])
def test_reported_total_requires_daylight_hours_and_preserves_missing_nights(
    values: dict[int, str | None], expected: float | None, source: str
) -> None:
    client, path, connection = connected_source()
    snapshot = archive_source(connection, "realtime_generation", values)
    response = client.get(path, {"date": "2020-01-01", "data_source": source})
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["reported_total_mwh"] == expected
    assert data["complete"] is False
    if values:
        assert data["readings"][23]["energy_mwh"] is None
        assert data["readings"][23]["reported"] is False
    snapshot.refresh_from_db()
    assert snapshot.complete is False
    assert len(snapshot.readings) == len(values)


def test_reported_total_uses_archived_location_and_selected_source_hours() -> None:
    client, path, connection = connected_source()
    archive_source(connection, "realtime_generation", dict.fromkeys(range(8, 18), "10"))
    archive_source(connection, "settlement_injection", {8: "0", 9: "7"})
    region = connection.plant.region
    region.latitude = 90
    region.save(update_fields=("latitude",))
    data = client.get(path, {"date": "2020-01-01"}).json()
    assert data["source_kind"] == "mixed"
    assert data["complete"] is False
    assert data["reported_total_mwh"] == 87
    assert data["readings"][8]["energy_mwh"] == 0


@pytest.mark.parametrize("hours", [19, 24])
def test_reported_total_requires_full_day_during_polar_night(hours: int) -> None:
    client, path, connection = connected_source()
    snapshot = archive_source(connection, "realtime_generation", dict.fromkeys(range(hours), "1"))
    binding = snapshot.configuration.binding
    binding.latitude = 90
    binding.save(update_fields=("latitude",))
    data = client.get(path, {"date": "2020-01-01"}).json()
    assert data["reported_total_mwh"] == (24 if hours == 24 else None)


def test_missing_values_in_both_sources_have_no_selected_acquisition() -> None:
    client, path, connection = connected_source()
    archive_source(connection, "realtime_generation", {8: None})
    archive_source(connection, "settlement_injection", {8: None})
    data = client.get(path, {"date": "2020-01-01"}).json()
    assert data["source_kind"] is None
    assert data["snapshot"] is None
    assert data["source_snapshots"] == []
    assert data["available_sources"] == []
    assert data["complete"] is False
    assert data["calibration_blocker"] == "production_unavailable"
    assert all(row["energy_mwh"] is None for row in data["readings"])
    assert all(row["source_kind"] is None for row in data["readings"])
    assert all(row["snapshot_id"] is None for row in data["readings"])


@pytest.mark.parametrize("invalid", ["-1", "NaN", "Infinity", "malformed"])
def test_invalid_settlement_energy_falls_back_and_never_makes_day_complete(invalid: str) -> None:
    client, path, connection = connected_source()
    realtime = archive_source(connection, "realtime_generation", {8: "2"})
    archive_source(connection, "settlement_injection", {8: invalid, 9: invalid})
    data = client.get(path, {"date": "2020-01-01"}).json()
    assert data["source_kind"] == "realtime_generation"
    assert data["complete"] is False
    assert data["readings"][8]["energy_mwh"] == 2
    assert data["readings"][8]["snapshot_id"] == realtime.pk
    assert data["readings"][9]["energy_mwh"] is None
