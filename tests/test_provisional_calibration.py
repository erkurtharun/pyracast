import json
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from django.db import connection as database
from django.utils import timezone

from pyracast.connections.contracts import ProviderData
from pyracast.epias import TIMEZONE, EpiasDay, EpiasPlant, EpiasReading
from pyracast.forecasts.jobs.claim import claim
from pyracast.forecasts.jobs.collect import persist_snapshot
from pyracast.forecasts.models import CollectionJob
from pyracast.intervals import NormalizedInterval
from pyracast.irradiance import local_day_bounds
from pyracast.metering.jobs import run_epias_job
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.plants.activation import activate_calibration
from pyracast.plants.history import plant_outlook
from pyracast.plants.measurement_policy import MEASUREMENT_POLICY
from pyracast.plants.models import CalibrationActivation, CalibrationRun, Plant, PlantConfiguration
from pyracast.plants.preparation import prepare_calibration
from pyracast.plants.training import run_calibration
from tests.support.domain import NOW, System
from tests.support.historical_production_identity import record_identity
from tests.support.jobs import publish
from tests.support.plant_operations import make_plant, publish_snapshot, snapshot_for
from tests.support.plant_training import actual_snapshot_for
from tests.support.settlement_calibration import settlement

pytestmark = pytest.mark.django_db(transaction=True)
FIRST_DAY = date(2026, 11, 1)


def reported(
    connection: EpiasConnection,
    configuration: PlantConfiguration,
    day: date,
    *,
    minute: int = 0,
) -> EpiasSnapshot:
    return settlement(
        connection,
        configuration,
        day,
        minute=minute,
        source_kind="realtime_generation",
        identity={
            "id": connection.provider_plant_id,
            "eic": connection.eic,
            "name": "Plant",
            "short_name": "Plant",
        },
    )


def test_provisional_fit_freezes_sources_then_settlement_prepares_a_new_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    days = [FIRST_DAY + timedelta(days=offset) for offset in range(8)]
    snapshots = []
    for day in days:
        actual_snapshot_for(system, day)
        snapshots.append(reported(connection, configuration, day))
    run = prepare_calibration(plant.pk)
    assert run is not None
    assert run.production_snapshot_ids == [snapshot.pk for snapshot in snapshots]
    assert run.measurement_selection_policy == MEASUREMENT_POLICY
    original_pins = list(run.production_snapshot_ids)
    original_cutoff = run.cutoff_at
    later = reported(connection, configuration, days[-1], minute=1)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "ready"
    assert run.factor is not None
    assert 0.5 < run.factor < 0.6
    assert run.result["valid_days"] == 8
    assert run.result["provisional_production"] is True
    assert run.result["production_sources"] == ["realtime_generation"]
    assert run.dataset_manifest["measurement_selection_policy"] == MEASUREMENT_POLICY
    observations = run.dataset_manifest["observations"]
    assert len(observations) == 8 * 24
    assert {row["production_snapshot_id"] for row in observations} == set(original_pins)
    assert later.pk not in {row["production_snapshot_id"] for row in observations}
    assert {row["production_source_kind"] for row in observations} == {"realtime_generation"}
    assert {row["production_basis"] for row in observations} == {"provisional_proxy"}
    assert all(
        row["production_digest"] and row["production_provider"]["eic"] == "EIC"
        for row in observations
    )
    assert not CalibrationActivation.objects.exists()
    assert not CalibrationRun.objects.filter(status="pending").exists()
    manifest = deepcopy(run.dataset_manifest)
    digest = run.dataset_sha256
    assert prepare_calibration(plant.pk) is None

    moment = NOW + timedelta(minutes=1)
    monkeypatch.setattr(timezone, "now", lambda: moment)
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)
    publish_snapshot(snapshot_for(system, date(2026, 11, 10), collected_at=moment))
    outlook = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=date(2026, 11, 10),
        resolution_minutes=60,
        as_of=moment,
    )
    numerical = cast(dict[str, Any], outlook["result"])
    assert outlook["calibration_id"] == run.pk
    assert "provisional_production_calibration" in numerical["quality_flags"]
    assert numerical["provenance"]["calibration_dataset_sha256"] == digest

    settled = [settlement(connection, configuration, day) for day in days]
    replacement = prepare_calibration(plant.pk)
    assert replacement is not None
    assert replacement.pk != run.pk
    assert replacement.production_snapshot_ids == [snapshot.pk for snapshot in settled]
    assert replacement.input_sha256 != run.input_sha256
    assert run_calibration(replacement.pk, moment)
    replacement.refresh_from_db()
    assert replacement.status == "ready"
    assert replacement.result["provisional_production"] is False
    assert replacement.result["production_sources"] == ["settlement_injection"]
    assert CalibrationActivation.objects.get().calibration_id == run.pk
    for day in days:
        settlement(connection, configuration, day, minute=1)
    assert prepare_calibration(plant.pk) is None
    assert CalibrationRun.objects.count() == 2
    run.refresh_from_db()
    assert run.production_snapshot_ids == original_pins
    assert run.cutoff_at == original_cutoff
    assert run.measurement_selection_policy == MEASUREMENT_POLICY
    assert run.dataset_manifest == manifest
    assert run.dataset_sha256 == digest


@pytest.mark.parametrize(
    "last_source", ["estimated_actuals", "realtime_complete", "realtime_partial"]
)
def test_last_publication_prepares_once_when_seven_daylight_days_become_matched(
    monkeypatch: pytest.MonkeyPatch, last_source: str
) -> None:
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
    for offset in range(6):
        day = FIRST_DAY + timedelta(days=offset)
        actual_snapshot_for(system, day)
        reported(connection, configuration, day)
    diagnostic = prepare_calibration(plant.pk)
    assert diagnostic is not None
    assert len(diagnostic.production_snapshot_ids) == 6
    assert run_calibration(diagnostic.pk, NOW)
    diagnostic.refresh_from_db()
    assert diagnostic.status == "insufficient"
    assert diagnostic.factor is None
    assert diagnostic.result["valid_days"] == 6
    assert diagnostic.result["included_day_dates"] == [
        (FIRST_DAY + timedelta(days=offset)).isoformat() for offset in range(6)
    ]
    assert not CalibrationActivation.objects.exists()
    last_day = FIRST_DAY + timedelta(days=6)
    start, _ = local_day_bounds(last_day, configuration.binding.timezone)
    dispatch = NOW - timedelta(seconds=1)
    if last_source == "estimated_actuals":
        reported(connection, configuration, last_day)
        assert prepare_calibration(plant.pk) is None
        job = CollectionJob.objects.create(
            binding=configuration.binding,
            target_date=last_day,
            endpoint="estimated_actuals",
            scheduled_for=dispatch,
            available_at=dispatch,
        )
        lease = claim(job.pk, dispatch)
        assert lease is not None
        data = ProviderData(
            {"fixture": "estimated_actuals"},
            NormalizedInterval.from_rows(
                [
                    {
                        "start": (start + timedelta(minutes=30 * step)).isoformat(),
                        "end": (start + timedelta(minutes=30 * (step + 1))).isoformat(),
                        "power_kw": 0.4,
                    }
                    for step in range(48)
                ]
            ),
            collected_at=dispatch,
            source_kind="estimated_actuals",
        )
        snapshot = persist_snapshot(lease, data, dispatch)
        assert snapshot is not None
        assert CalibrationRun.objects.count() == 1
        assert not CalibrationRun.objects.filter(status="pending").exists()
        assert publish(lease, snapshot, {}, dispatch)
        assert not publish(lease, snapshot, {}, dispatch)
    else:
        actual_snapshot_for(system, last_day)
        assert prepare_calibration(plant.pk) is None
        production_job = EpiasCollectionJob.objects.create(
            plant_id=connection.plant_id,
            connection=connection,
            configuration=configuration,
            connection_revision=connection.revision,
            provider_plant={"id": 2, "eic": "EIC", "name": "Plant", "short_name": "Plant"},
            target_date=last_day,
            scheduled_for=dispatch,
            available_at=dispatch,
        )

        def fetch(provider: EpiasPlant, day: date) -> EpiasDay:
            assert not database.in_atomic_block
            return EpiasDay(
                provider,
                day,
                tuple(
                    EpiasReading(
                        start.astimezone(TIMEZONE) + timedelta(hours=hour),
                        f"{hour:02}:00",
                        Decimal(26),
                        None,
                    )
                    for hour in range(23 if last_source == "realtime_partial" else 24)
                ),
                dispatch,
                json.dumps({"items": [{"total": "26"}]}),
            )

        succeeded = run_epias_job(production_job.pk, fetch=fetch, now=dispatch)
        production_job.refresh_from_db()
        assert succeeded is (last_source == "realtime_complete"), production_job.last_error
        assert production_job.status == (
            "pending" if last_source == "realtime_partial" else "succeeded"
        )
        assert not run_epias_job(production_job.pk, fetch=fetch, now=dispatch)
    run = CalibrationRun.objects.get(status="pending")
    assert run.pk != diagnostic.pk
    assert run.status == "pending"
    assert run.window_start == FIRST_DAY
    assert run.window_end == last_day
    assert len(run.production_snapshot_ids) == 7
    assert run.measurement_selection_policy == MEASUREMENT_POLICY
    assert prepare_calibration(plant.pk) is None
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "ready"
    assert run.result["valid_days"] == 7
    assert run.result["provisional_production"] is True
    assert not CalibrationActivation.objects.exists()
    assert CalibrationRun.objects.count() == 2
    assert prepare_calibration(plant.pk) is None
    diagnostic.refresh_from_db()
    assert diagnostic.status == "insufficient"
    assert diagnostic.result["valid_days"] == 6


def fitted_production(
    monkeypatch: pytest.MonkeyPatch, *, settled: bool = False
) -> tuple[System, Plant, EpiasConnection, CalibrationRun]:
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
    for offset in range(8):
        day = FIRST_DAY + timedelta(days=offset)
        actual_snapshot_for(system, day)
        reported(connection, configuration, day)
        if settled:
            settlement(connection, configuration, day)
    run = prepare_calibration(plant.pk)
    assert run is not None
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "ready"
    return system, plant, connection, run


@pytest.mark.parametrize("changed_identity", ["eic", "provider_plant_id"])
def test_changed_production_identity_uses_baseline_and_preserves_archived_forecast(
    monkeypatch: pytest.MonkeyPatch, changed_identity: str
) -> None:
    system, plant, connection, run = fitted_production(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    moment = NOW + timedelta(minutes=1)
    monkeypatch.setattr(timezone, "now", lambda: moment)
    activation = activate_calibration(
        system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk
    )
    target = date(2026, 11, 10)
    publish_snapshot(snapshot_for(system, target, collected_at=moment))
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    historical_cutoff = moment
    archived = plant_outlook(
        *args, target_date=target, resolution_minutes=60, as_of=historical_cutoff
    )
    live = plant_outlook(*args, target_date=target, resolution_minutes=60)
    assert archived["calibration_id"] == run.pk
    assert live["calibration_id"] == run.pk
    assert cast(dict[str, Any], live["result"])["factor"] == run.factor

    setattr(connection, changed_identity, "OTHER" if changed_identity == "eic" else 3)
    connection.save(update_fields=(changed_identity,))
    moment = NOW + timedelta(minutes=2)
    baseline = plant_outlook(*args, target_date=target, resolution_minutes=60)
    numerical = cast(dict[str, Any], baseline["result"])
    assert baseline["calibration_id"] is None
    assert numerical["factor"] == 1
    assert numerical["energy_mwh"] == 1248
    assert "uncalibrated" in numerical["quality_flags"]
    assert "provisional_production_calibration" not in numerical["quality_flags"]
    replay = plant_outlook(
        *args, target_date=target, resolution_minutes=60, as_of=historical_cutoff
    )
    assert replay == archived
    for offset in range(8):
        reported(connection, configuration, FIRST_DAY + timedelta(days=offset), minute=2)
    replacement = prepare_calibration(plant.pk)
    assert replacement is not None
    assert replacement.comparison_activation_id == activation.pk
    assert run_calibration(replacement.pk, moment)
    replacement.refresh_from_db()
    assert replacement.status == "ready"
    assert replacement.result["incumbent_factor"] == 1
    assert replacement.dataset_manifest["incumbent_factor"] == 1
    assert CalibrationActivation.objects.get().calibration_id == run.pk


def test_empty_settlement_correction_automatically_prepares_provisional_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plant, connection, run = fitted_production(monkeypatch, settled=True)
    configuration = plant.current_configuration
    assert configuration is not None
    assert run.result["provisional_production"] is False
    old_manifest = deepcopy(run.dataset_manifest)
    old_digest = run.dataset_sha256
    last_day = FIRST_DAY + timedelta(days=7)
    realtime = EpiasSnapshot.objects.get(
        configuration=configuration, target_date=last_day, source_kind="realtime_generation"
    )
    old_settlement = EpiasSnapshot.objects.get(
        configuration=configuration, target_date=last_day, source_kind="settlement_injection"
    )
    dispatch = NOW - timedelta(seconds=1)
    job = EpiasCollectionJob.objects.create(
        plant_id=connection.plant_id,
        connection=connection,
        configuration=configuration,
        connection_revision=connection.revision,
        source_kind="settlement_injection",
        provider_plant={"id": 77, "eic": "EIC", "name": "Plant", "short_name": "Plant"},
        target_date=last_day,
        scheduled_for=dispatch,
        available_at=dispatch,
    )

    def fetch(provider: EpiasPlant, day: date) -> EpiasDay:
        assert not database.in_atomic_block
        return EpiasDay(
            provider, day, (), dispatch, '{"items": []}', source_kind="settlement_injection"
        )

    assert not run_epias_job(job.pk, fetch=fetch, now=dispatch)
    job.refresh_from_db()
    assert job.status == "awaiting_publication"
    empty = EpiasSnapshot.objects.get(job=job)
    assert empty.readings == []
    replacement = CalibrationRun.objects.get(status="pending")
    assert replacement.pk != run.pk
    assert replacement.input_sha256 != run.input_sha256
    assert realtime.pk in replacement.production_snapshot_ids
    assert old_settlement.pk not in replacement.production_snapshot_ids
    assert empty.pk not in replacement.production_snapshot_ids
    assert run_calibration(replacement.pk, NOW)
    replacement.refresh_from_db()
    assert replacement.status == "ready"
    assert replacement.result["valid_days"] == 8
    assert replacement.result["provisional_production"] is True
    assert replacement.result["production_sources"] == [
        "realtime_generation",
        "settlement_injection",
    ]
    assert not CalibrationActivation.objects.exists()
    assert CalibrationRun.objects.count() == 2
    run.refresh_from_db()
    assert run.dataset_manifest == old_manifest
    assert run.dataset_sha256 == old_digest
