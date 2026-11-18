from datetime import date, timedelta
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasConnection
from pyracast.plants import training
from pyracast.plants.measurements import (
    latest_measurement_day,
    production_identity_current,
    select_measurements,
    selection_fingerprint,
)
from pyracast.plants.models import CalibrationRun, ProductionObservation
from pyracast.plants.numerics import FitResult, fit_calibration
from pyracast.plants.operations import activate_calibration
from pyracast.plants.preparation import prepare_calibration
from tests.support.domain import NOW
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant
from tests.support.plant_training import actual_snapshot_for
from tests.support.settlement_calibration import settlement

pytestmark = pytest.mark.django_db(transaction=True)


def test_istanbul_snapshots_cover_a_london_local_day(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    first = settlement(connection, config, date(2026, 11, 7))
    second = settlement(connection, config, date(2026, 11, 8))
    config.binding.timezone = "Europe/London"
    start, end = local_day_bounds(date(2026, 11, 7), "Europe/London")
    rows, ids = select_measurements(config, start, end, NOW)
    assert len(rows) == 24
    assert ids == [first.pk, second.pk]
    assert rows[0].starts_at == start
    assert rows[-1].ends_at == end
    first.delete()
    stored_again(second, readings=second.readings[:3])
    assert latest_measurement_day(config, end, NOW, 0) == date(2026, 11, 7)


def test_selection_keeps_quality_override_and_excludes_stale_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    day = date(2026, 11, 1)
    start, end = local_day_bounds(day, config.binding.timezone)
    old = settlement(connection, config, day)
    manual = ProductionObservation.objects.create(
        plant=plant,
        configuration=config,
        starts_at=start + timedelta(hours=12),
        ends_at=start + timedelta(hours=12, minutes=30),
        energy_mwh=3,
        quality="invalid",
        source="manual",
        recorded_by=system.member,
    )
    rows, ids = select_measurements(config, start, end, NOW)
    assert prepare_calibration(plant.pk) is None
    assert ids == [old.pk]
    assert len(rows) == 24
    assert [(r.starts_at, r.quality) for r in rows if r.pk == manual.pk] == [
        (manual.starts_at, "invalid")
    ]
    assert not any(r.snapshot and r.starts_at == manual.starts_at for r in rows)
    first, _ = selection_fingerprint(config, rows, NOW)
    later = settlement(connection, config, day, minute=1)
    refreshed, _ = select_measurements(config, start, end, NOW)
    assert selection_fingerprint(config, refreshed, NOW)[0] == first
    pinned, pinned_ids = select_measurements(
        config, start, end, NOW, production_snapshot_ids=[old.pk]
    )
    assert pinned_ids == [old.pk]
    assert pinned[0].snapshot is not None
    assert pinned[0].snapshot.pk == old.pk
    changed = settlement(connection, config, day, value=27, minute=2)
    revised, ids = select_measurements(config, start, end, NOW)
    assert ids == [changed.pk]
    assert selection_fingerprint(config, revised, NOW)[0] != first
    connection.eic = "OTHER"
    connection.save()
    assert select_measurements(config, start, end, NOW)[1] == []
    assert later.pk != changed.pk


def test_settlement_only_automatically_prepares_and_pins_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    snapshots = []
    for offset in range(7):
        day = date(2026, 11, 1) + timedelta(days=offset)
        actual_snapshot_for(system, day)
        snapshots.append(settlement(connection, config, day))
    stored_again(settlement(connection, config, date(2026, 11, 8)), readings=[])
    stored_again(settlement(connection, config, date(2025, 1, 1)), readings=[None])
    run = prepare_calibration(plant.pk)
    assert run is not None
    assert run.requested_by == system.member
    assert run.window_end == date(2026, 11, 7)
    assert run.production_snapshot_ids == [s.pk for s in snapshots]
    later = settlement(connection, config, date(2026, 11, 7), value=27, minute=1)
    assert training.run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "ready"
    assert {row["production_snapshot_id"] for row in run.dataset_manifest["observations"]} == {
        s.pk for s in snapshots
    }
    followup = CalibrationRun.objects.get(status="pending")
    assert later.pk in followup.production_snapshot_ids
    assert training.run_calibration(followup.pk, NOW + timedelta(seconds=10))
    settlement(connection, config, date(2026, 11, 7), value=27, minute=2)
    assert prepare_calibration(plant.pk) is None
    assert not CalibrationRun.objects.filter(status="pending").exists()


def test_changed_settlement_identity_cannot_publish_or_activate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    for offset in range(7):
        day = date(2026, 11, 1) + timedelta(days=offset)
        actual_snapshot_for(system, day)
        settlement(connection, config, day)
    run = prepare_calibration(plant.pk)
    assert run is not None
    original_fit = fit_calibration

    def change_identity(*args: Any, **kwargs: Any) -> FitResult:
        result = original_fit(*args, **kwargs)
        connection.eic = "OTHER"
        connection.save()
        return result

    monkeypatch.setattr(training, "fit_calibration", change_identity)
    assert not training.run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "superseded"
    connection.eic = "EIC"
    connection.save()
    monkeypatch.setattr(training, "fit_calibration", original_fit)
    ready = prepare_calibration(plant.pk)
    assert ready is not None
    assert training.run_calibration(ready.pk, NOW + timedelta(seconds=10))
    ready.refresh_from_db()
    assert ready.status == "ready"
    connection.eic = "OTHER"
    connection.save()
    with pytest.raises(ValidationError, match="production source changed"):
        activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, ready.pk)


def test_partial_latest_settlement_preserves_missing_and_rejects_invalid_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    day = date(2026, 11, 1)
    start, end = local_day_bounds(day, config.binding.timezone)
    assert select_measurements(config, start, end, NOW) == ([], [])
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    settlement(connection, config, day)
    latest = settlement(connection, config, day, minute=1)
    valid = latest.readings[0]
    readings = [
        None,
        {},
        {"total_mwh": "1", "period_start": "bad", "period_end": "bad"},
        {
            "total_mwh": "-1",
            "period_start": valid["period_start"],
            "period_end": valid["period_end"],
        },
        {
            "total_mwh": "1",
            "period_start": "2026-11-01T00:00:00",
            "period_end": "2026-11-01T01:00:00",
        },
        valid,
    ]
    stored_again(latest, readings=readings, complete=False)
    rows, ids = select_measurements(config, start, end, NOW)
    assert ids == [latest.pk]
    assert len(rows) == 1
    assert rows[0].energy_mwh == 26
    config.measurement_basis = "ac_generation"
    assert select_measurements(config, start, end, NOW) == ([], [])
    config.measurement_basis = "net_export"
    connection.settlement_plant_id = None
    connection.save(update_fields=("settlement_plant_id",))
    assert select_measurements(config, start, end, NOW) == ([], [])


def test_identity_guard_handles_missing_or_disconnected_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    run = CalibrationRun(plant=plant, configuration=config, production_snapshot_ids=[123456])
    assert not production_identity_current(run)
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    assert not production_identity_current(run)
    connection.settlement_plant_id = 77
    connection.enabled = False
    connection.save()
    assert not production_identity_current(run)
    snapshot = settlement(connection, config, date(2026, 11, 1))
    run.production_snapshot_ids = [snapshot.pk]
    assert production_identity_current(run)
    connection.settlement_plant_id = 78
    connection.save()
    assert not production_identity_current(run)
