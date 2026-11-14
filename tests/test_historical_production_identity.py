from datetime import datetime, timedelta

import pytest
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.plants.measurements import production_identity_at, production_identity_current
from pyracast.plants.models import CalibrationRun, Plant, PlantEvent
from pyracast.plants.operations import activate_calibration
from pyracast.regions.models import Region
from tests.test_domain import NOW
from tests.test_plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for
from tests.test_provisional_measurements import acquisition

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_automatic_forecast_keeps_the_day_end_factor_across_midnight(
    monkeypatch: pytest.MonkeyPatch, after_cutoff: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    record_identity(monkeypatch, connection, NOW)
    production = acquisition((configuration, connection))
    run = ready_run(system, plant)
    run.production_snapshot_ids = [production.pk]
    run.save(update_fields=("production_snapshot_ids",))
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)
    day = NOW.date()
    publish_snapshot(snapshot_for(system, day, power_kw=0.1, collected_at=NOW))
    _, end = local_day_bounds(day, configuration.binding.timezone)
    cutoff = end - timedelta(microseconds=1)
    if not after_cutoff:
        connection.provider_plant_id = 3
        connection.revision += 1
        record_identity(monkeypatch, connection, cutoff - timedelta(minutes=30))
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    query = {"date": day.isoformat(), "data_source": "auto"}
    monkeypatch.setattr(timezone, "now", lambda: cutoff)
    before = client.get(path, query)
    assert before.status_code == 200, before.content
    expected_factor = 0.5 if after_cutoff else 1
    assert before.json()["result"]["factor"] == expected_factor
    if after_cutoff:
        connection.provider_plant_id = 3
        connection.revision += 1
        record_identity(monkeypatch, connection, end + timedelta(minutes=30))
    monkeypatch.setattr(timezone, "now", lambda: end + timedelta(hours=1))
    after = client.get(path, query)
    assert after.status_code == 200, after.content
    assert after.json()["source_kind"] == "forecast"
    assert after.json()["result"]["factor"] == expected_factor
    assert after.json()["result"]["intervals"] == before.json()["result"]["intervals"]
    assert "forecast_fallback" in after.json()["result"]["quality_flags"]


def historical_source(
    monkeypatch: pytest.MonkeyPatch, kind: str = "realtime_generation"
) -> tuple[CalibrationRun, EpiasConnection, EpiasSnapshot, datetime]:
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
    record_identity(monkeypatch, connection, NOW)
    snapshot = acquisition((configuration, connection), kind)
    run = CalibrationRun(
        plant=plant, configuration=configuration, production_snapshot_ids=[snapshot.pk]
    )
    _, end = local_day_bounds(NOW.date(), configuration.binding.timezone)
    return run, connection, snapshot, end - timedelta(microseconds=1)


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


@pytest.mark.parametrize("kind", ["realtime_generation", "settlement_injection"])
@pytest.mark.parametrize("after_cutoff", [False, True])
def test_day_end_identity_is_frozen_before_later_connection_changes(
    monkeypatch: pytest.MonkeyPatch, kind: str, after_cutoff: bool
) -> None:
    run, connection, _, cutoff = historical_source(monkeypatch, kind)
    assert production_identity_current(run)
    assert production_identity_at(run, cutoff)
    connection.provider_plant_id = 3
    connection.settlement_plant_id = 88
    connection.eic = "OTHER"
    connection.revision += 1
    stamp = cutoff + timedelta(minutes=30) if after_cutoff else cutoff - timedelta(minutes=30)
    record_identity(monkeypatch, connection, stamp)
    assert not production_identity_current(run)
    assert production_identity_at(run, cutoff) is after_cutoff


def test_later_identity_restoration_cannot_validate_an_invalid_day_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, connection, _, cutoff = historical_source(monkeypatch)
    connection.provider_plant_id = 3
    connection.revision += 1
    record_identity(monkeypatch, connection, cutoff - timedelta(minutes=30))
    connection.provider_plant_id = 2
    connection.revision += 1
    record_identity(monkeypatch, connection, cutoff + timedelta(minutes=30))
    assert production_identity_current(run)
    assert not production_identity_at(run, cutoff)


def test_settlement_requires_its_own_identity_evidence_before_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, connection, snapshot, cutoff = historical_source(monkeypatch, "settlement_injection")
    connection.provider_plant_id = 3
    connection.revision += 1
    record_identity(monkeypatch, connection, cutoff - timedelta(minutes=30))
    assert production_identity_current(run)
    assert not production_identity_at(run, cutoff)
    job = snapshot.job
    job.pk = None
    job.connection_revision = connection.revision
    job.save()
    assert production_identity_at(run, cutoff)
    EpiasCollectionJob.objects.filter(pk=job.pk).update(created_at=cutoff + timedelta(seconds=1))
    assert not production_identity_at(run, cutoff)


def test_conflicting_settlement_job_identity_is_not_silently_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _, snapshot, cutoff = historical_source(monkeypatch, "settlement_injection")
    job = snapshot.job
    job.pk = None
    job.scheduled_for += timedelta(seconds=1)
    job.provider_plant = {"id": 88, "eic": "EIC"}
    job.save()
    assert not production_identity_at(run, cutoff)
    for provider in ({"id": False, "eic": "EIC"}, {"id": 77, "eic": "OTHER"}):
        job.provider_plant = provider
        job.save(update_fields=("provider_plant",))
        assert not production_identity_at(run, cutoff)


def test_invalid_event_identity_cannot_authorize_a_saved_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _, _, cutoff = historical_source(monkeypatch)
    events = PlantEvent.objects.filter(plant_id=run.plant_id, action="epias_connected")
    for key, invalid in (
        ("revision", True),
        ("revision", "1"),
        ("revision", 0),
        ("provider_plant_id", False),
        ("provider_plant_id", -1),
        ("eic", None),
        ("eic", ""),
    ):
        events.update(details={"revision": 1, "provider_plant_id": 2, "eic": "EIC", key: invalid})
        assert not production_identity_at(run, cutoff)


def test_pinned_source_identity_and_service_must_be_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _, snapshot, cutoff = historical_source(monkeypatch)
    for identifier in (None, True, 0):
        snapshot.provider_plant = {"id": identifier, "eic": "EIC"}
        snapshot.save(update_fields=("provider_plant",))
        assert not production_identity_at(run, cutoff)
    snapshot.provider_plant = {"id": 2, "eic": "EIC"}
    snapshot.save(update_fields=("provider_plant",))
    snapshot.job.source_kind = "settlement_injection"
    snapshot.job.save(update_fields=("source_kind",))
    assert not production_identity_at(run, cutoff)


@pytest.mark.parametrize("change", ["event", "source", "publication", "policy", "malformed_event"])
def test_historical_identity_rejects_missing_or_mismatched_evidence(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    run, _, snapshot, cutoff = historical_source(monkeypatch)
    if change == "event":
        PlantEvent.objects.filter(plant_id=run.plant_id, action="epias_connected").delete()
    elif change == "source":
        run.production_snapshot_ids = [snapshot.pk + 1000]
    elif change == "publication":
        snapshot.available_at = cutoff + timedelta(seconds=1)
        snapshot.save(update_fields=("available_at",))
    elif change == "policy":
        snapshot.normalization_policy = "unknown"
        snapshot.save(update_fields=("normalization_policy",))
    else:
        PlantEvent.objects.filter(plant_id=run.plant_id, action="epias_connected").update(
            details={}
        )
    assert not production_identity_at(run, cutoff)


def test_historical_identity_cannot_use_another_tenants_matching_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, connection, _, cutoff = historical_source(monkeypatch)
    other_region = Region.objects.create(
        tenant=Tenant.objects.create(name="Other tenant"),
        name="Other region",
        latitude=40,
        longitude=29,
        timezone="Europe/Istanbul",
    )
    other_plant = Plant.objects.create(region=other_region, name="Other plant")
    other_connection = EpiasConnection.objects.create(
        plant=other_plant,
        provider_plant_id=connection.provider_plant_id,
        eic=connection.eic,
        name="Same provider identity",
        connected_by=connection.connected_by,
    )
    record_identity(monkeypatch, other_connection, NOW)
    run.plant = other_plant
    assert not production_identity_at(run, cutoff)


def test_manual_only_calibration_does_not_require_a_provider_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plant = make_plant(monkeypatch)
    run = CalibrationRun(plant=plant, configuration=plant.current_configuration)
    assert not EpiasConnection.objects.filter(plant=plant).exists()
    assert production_identity_current(run)
    assert production_identity_at(run, NOW)
