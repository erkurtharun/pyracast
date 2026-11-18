from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, timedelta
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.incidents.operations import save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasConnection
from pyracast.plants.forecast_archive import (
    ARCHIVE_SELECTION_POLICY,
    forecast_cutoff,
    issuance_power_intervals,
    prepare_forecast_issuances,
    save_forecast_issuances,
    select_forecast_issuance,
)
from pyracast.plants.models import (
    CalibrationActivation,
    ForecastIssuance,
    Plant,
    PlantConfiguration,
    PlantEvent,
)
from pyracast.trash import RETENTION, purge_expired, trash_region
from tests.support.domain import NOW
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant, ready_run, snapshot_for
from tests.support.provisional_measurements import acquisition

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 10)


def freeze(snapshot: ForecastSnapshot, plant: Plant) -> ForecastIssuance:
    prepared = prepare_forecast_issuances(snapshot)
    with transaction.atomic():
        locked = Plant.objects.select_for_update().get(pk=plant.pk)
        save_forecast_issuances(prepared, snapshot, snapshot.collected_at, plant=locked)
    return ForecastIssuance.objects.get(plant=plant, snapshot=snapshot, target_date=DAY)


def test_latest_pre_daylight_acquisition_wins_without_actuals_or_job_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    sources = [
        snapshot_for(system, DAY, collected_at=start - timedelta(hours=hours))
        for hours in (10, 8, 7)
    ]
    for source in sources:
        freeze(source, plant)
    selected = select_forecast_issuance(plant, DAY)
    assert selected is not None
    assert selected.snapshot_id == sources[-1].pk
    before_sunrise = snapshot_for(system, DAY, collected_at=start + timedelta(hours=3))
    before_sunrise.job.target_date = DAY + timedelta(days=1)
    before_sunrise.job.save(update_fields=("target_date",))
    before_sunrise.normalized_intervals = before_sunrise.normalized_intervals[:1]
    before_sunrise.normalized_intervals[0]["power_kw"] = None
    stored_again(before_sunrise)
    latest = freeze(before_sunrise, plant)
    assert select_forecast_issuance(plant, DAY) == latest
    assert latest.payloads["60"]["complete"] is False
    assert issuance_power_intervals(latest)[0].power_mw is None
    selected = select_forecast_issuance(plant, DAY, as_of=sources[-1].collected_at)
    assert selected is not None
    assert selected.snapshot_id == sources[-1].pk
    assert select_forecast_issuance(plant, DAY, binding_id=system.binding.pk) == latest
    assert select_forecast_issuance(plant, DAY, binding_id=system.binding.pk + 1) is None
    assert select_forecast_issuance(plant, DAY + timedelta(days=1)) is None
    intraday = snapshot_for(system, DAY, collected_at=start + timedelta(hours=10))
    actual = snapshot_for(system, DAY, source_kind="estimated_actuals")
    assert prepare_forecast_issuances(intraday) == ()
    assert prepare_forecast_issuances(actual) == ()
    assert select_forecast_issuance(plant, DAY) == latest


def test_calibration_and_native_clipping_are_frozen_at_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = ready_run(system, plant)
    run.created_at = run.completed_at = run.cutoff_at = NOW - timedelta(hours=2)
    run.save()
    monkeypatch.setattr(timezone, "now", lambda: NOW - timedelta(hours=1))
    CalibrationActivation.objects.create(
        plant=plant, configuration=run.configuration, calibration=run
    )
    source = snapshot_for(system, DAY, power_kw=120 / 130, collected_at=NOW)
    source.normalized_intervals[1]["power_kw"] = 80 / 130
    source.normalized_intervals[2]["power_kw"] = None
    stored_again(source)
    prepared = prepare_forecast_issuances(source)
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(minutes=1))
    CalibrationActivation.objects.create(plant=plant, configuration=run.configuration)
    with transaction.atomic():
        save_forecast_issuances(prepared, source, NOW, plant=plant)
    issuance = select_forecast_issuance(plant, DAY)
    assert issuance is not None
    assert issuance.calibration_id == run.pk
    assert issuance.factor == 0.5
    assert [row.power_mw for row in issuance_power_intervals(issuance)[:3]] == [60, 40, None]
    assert [row.power_mw for row in issuance_power_intervals(issuance, baseline=True)[:3]] == [
        100,
        80,
        None,
    ]
    assert issuance.payloads["60"]["intervals"][0]["energy_mwh"] == 50
    assert issuance.payloads["30"]["provenance"]["selection_policy"] == ARCHIVE_SELECTION_POLICY
    assert issuance.payloads["30"]["provenance"]["published_at"] == NOW.isoformat()
    original = deepcopy(issuance.payloads)
    source.normalized_intervals[0]["power_kw"] = 0
    stored_again(source)
    with transaction.atomic():
        save_forecast_issuances(
            tuple(replace(item, factor=1.0) for item in prepared), source, NOW, plant=plant
        )
    issuance.refresh_from_db()
    assert issuance.payloads == original
    assert issuance.factor == 0.5
    with pytest.raises(IntegrityError, match="immutable"), transaction.atomic():
        ForecastIssuance.objects.filter(pk=issuance.pk).update(factor=1.0)


def test_new_acquisition_uses_identity_known_then_and_preserves_older_issuance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    configuration = PlantConfiguration.objects.get(pk=plant.current_configuration_id)
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    PlantEvent.objects.create(
        plant=plant,
        actor=system.member,
        action="epias_connected",
        details={"provider_plant_id": 2, "eic": "EIC", "revision": connection.revision},
    )
    production = acquisition((configuration, connection))
    run = ready_run(system, plant)
    run.production_snapshot_ids = [production.pk]
    run.save(update_fields=("production_snapshot_ids",))
    CalibrationActivation.objects.create(plant=plant, configuration=configuration, calibration=run)
    source = snapshot_for(system, DAY, collected_at=NOW)
    frozen = freeze(source, plant)
    assert frozen.factor == 0.5
    assert frozen.calibration_id == run.pk
    original = deepcopy(frozen.payloads)
    moment = NOW + timedelta(minutes=1)
    monkeypatch.setattr(timezone, "now", lambda: moment)
    connection.provider_plant_id = 3
    connection.revision += 1
    connection.save(update_fields=("provider_plant_id", "revision"))
    PlantEvent.objects.create(
        plant=plant,
        actor=system.member,
        action="epias_connected",
        details={"provider_plant_id": 3, "eic": "EIC", "revision": connection.revision},
    )
    later = snapshot_for(system, DAY, collected_at=moment)
    updated = freeze(later, plant)
    assert updated.factor == 1.0
    assert updated.calibration_id is None
    assert "uncalibrated" in updated.payloads["30"]["quality_flags"]
    replay = select_forecast_issuance(plant, DAY, as_of=NOW)
    assert replay is not None
    assert replay.pk == frozen.pk
    assert replay.payloads == original
    assert prepare_forecast_issuances(source)[0].calibration_id == run.pk


def test_covered_days_keep_native_boundaries_without_using_job_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    source = snapshot_for(system, DAY, collected_at=start - timedelta(hours=4))
    source.normalized_intervals = [
        {
            "start": (start + timedelta(days=1)).isoformat(),
            "end": (start + timedelta(days=1, hours=1)).isoformat(),
            "power_kw": 0,
        },
        {
            "start": (start - timedelta(minutes=15)).isoformat(),
            "end": (start + timedelta(minutes=15)).isoformat(),
            "power_kw": 0.4,
        },
    ]
    stored_again(source)
    prepared = prepare_forecast_issuances(source)
    assert [item.target_date for item in prepared] == [DAY, DAY + timedelta(days=1)]
    first = prepared[0].native_intervals
    assert len(first) == 1
    assert first[0]["start"] == start.isoformat()
    assert first[0]["original_start"] == (start - timedelta(minutes=15)).isoformat()
    assert first[0]["power_mw"] == 52
    assert prepared[1].native_intervals[0]["power_mw"] == 0
    with transaction.atomic():
        save_forecast_issuances(prepared, source, source.collected_at, plant=plant)
    issuance = select_forecast_issuance(plant, DAY)
    assert issuance is not None
    assert issuance.payloads["30"]["partial_energy_mwh"] == 13


def test_saved_outage_keeps_timestamp_precision_and_archive_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    recorded_at = NOW.replace(microsecond=123456)
    starts_at = (NOW - timedelta(minutes=1)).replace(microsecond=246810)
    monkeypatch.setattr(timezone, "now", lambda: recorded_at)
    save_incident(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        kind="panels",
        title="Reduced capacity",
        notes="",
        starts_at=starts_at,
        ends_at=None,
        available_dc_mw=65,
        available_inverter_mw=None,
        available_export_mw=100,
    )
    source = snapshot_for(system, DAY, collected_at=NOW.replace(microsecond=654321))
    issuance = freeze(source, plant)
    for resolution in ("30", "60"):
        scenario = issuance.outage_scenarios[resolution]
        assert scenario is not None
        assert scenario["incidents"][0]["recorded_at"] == recorded_at.isoformat()
        assert scenario["incidents"][0]["starts_at"] == starts_at.isoformat()
        assert scenario["incidents"][0]["ends_at"] is None
        assert scenario["incidents"][0]["cancelled_at"] is None
        assert scenario["result"]["provenance"] == (
            issuance.payloads[resolution]["provenance"]
            | {
                "scenario_policy": "reported-operating-limits-v1",
                "evaluated_at": source.collected_at.isoformat(),
                "incident_revision_ids": scenario["incident_revision_ids"],
            }
        )
        assert scenario["result"]["provenance"]["selection_policy"] == ARCHIVE_SELECTION_POLICY
        assert scenario["result"]["energy_mwh"] == 624
        assert issuance.payloads[resolution]["energy_mwh"] == 1248


def test_acquisition_issue_and_publication_must_precede_daylight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    cutoff = forecast_cutoff(DAY, system.binding)
    exact = snapshot_for(system, DAY, collected_at=cutoff)
    assert prepare_forecast_issuances(exact) == ()
    issued = snapshot_for(system, DAY, issued_at=cutoff)
    assert prepare_forecast_issuances(issued) == ()
    issued_before_sunrise = snapshot_for(
        system,
        DAY,
        collected_at=cutoff - timedelta(hours=1),
        issued_at=cutoff - timedelta(minutes=30),
    )
    assert prepare_forecast_issuances(issued_before_sunrise) == ()
    source = snapshot_for(system, DAY)
    source.issued_at = source.collected_at
    stored_again(source)
    prepared = prepare_forecast_issuances(source)
    for published in (source.collected_at - timedelta(seconds=1), cutoff):
        with transaction.atomic():
            save_forecast_issuances(prepared, source, published, plant=plant)
    assert not ForecastIssuance.objects.exists()
    with transaction.atomic():
        save_forecast_issuances(prepared, source, source.collected_at, plant=plant)
    assert select_forecast_issuance(plant, DAY) is not None
    with pytest.raises(ValueError, match="timezone-aware"):
        save_forecast_issuances(prepared, source, NOW.replace(tzinfo=None), plant=plant)
    with pytest.raises(ValueError, match="timezone-aware"):
        select_forecast_issuance(plant, DAY, as_of=NOW.replace(tzinfo=None))


def test_preparation_excludes_missing_or_later_configuration_and_mixed_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = snapshot_for(system, DAY)
    prepared = prepare_forecast_issuances(source)
    assert len(prepared) == 1
    for absent in (
        None,
        replace_plant(plant, current_configuration_id=None),
    ):
        save_forecast_issuances(prepared, source, source.collected_at, plant=absent)
    assert not ForecastIssuance.objects.exists()
    original_kind = source.source_kind
    source.source_kind = "estimated_actuals"
    with pytest.raises(ValueError, match="must match"):
        save_forecast_issuances(prepared, source, source.collected_at, plant=plant)
    source.source_kind = original_kind
    plant.current_configuration = None
    plant.save(update_fields=("current_configuration",))
    assert prepare_forecast_issuances(source) == ()
    plant.current_configuration_id = prepared[0].configuration_id
    plant.save(update_fields=("current_configuration",))
    configuration = PlantConfiguration.objects.get(pk=prepared[0].configuration_id)
    configuration.created_at = source.collected_at + timedelta(seconds=1)
    configuration.save(update_fields=("created_at",))
    assert prepare_forecast_issuances(source) == ()
    configuration.created_at = source.collected_at - timedelta(seconds=1)
    configuration.save(update_fields=("created_at",))
    source.normalized_intervals[0]["source_kind"] = "estimated_actuals"
    assert prepare_forecast_issuances(source) == ()


def replace_plant(plant: Plant, **changes: Any) -> Plant:
    copy = deepcopy(plant)
    for field, value in changes.items():
        setattr(copy, field, value)
    return copy


@pytest.mark.parametrize(
    ("day", "latitude", "expected_hour"),
    [(date(2026, 6, 21), 80.0, 0), (date(2026, 12, 21), 80.0, 0)],
)
def test_polar_cutoff_is_local_day_start(day: date, latitude: float, expected_hour: int) -> None:
    binding = SiteBinding(timezone="Europe/Oslo", latitude=latitude, longitude=18)
    expected, _ = local_day_bounds(day, binding.timezone)
    assert forecast_cutoff(day, binding) == expected
    assert expected.astimezone(UTC).hour != expected_hour


@pytest.mark.parametrize(
    "rows",
    [
        {},
        [None],
        [{"start": None, "end": "2026-11-10T01:00:00+00:00"}],
        [{"start": "2026-11-10T00:00:00", "end": "2026-11-10T01:00:00+00:00"}],
        [{"start": "2026-11-10T01:00:00+00:00", "end": "2026-11-10T00:00:00+00:00"}],
        [
            {"start": "2026-11-10T00:00:00+00:00", "end": "2026-11-10T01:00:00+00:00"},
            {"start": "2026-11-10T00:00:00+00:00", "end": "2026-11-10T01:00:00+00:00"},
        ],
        [
            {
                "start": "2026-11-10T00:00:00+00:00",
                "end": "2026-11-10T01:00:00+00:00",
                "power_mw": True,
            }
        ],
        [
            {
                "start": "2026-11-10T00:00:00+00:00",
                "end": "2026-11-10T01:00:00+00:00",
                "power_mw": -1,
            }
        ],
        [
            {
                "start": "2026-11-10T00:00:00+00:00",
                "end": "2026-11-10T01:00:00+00:00",
                "power_mw": float("inf"),
            }
        ],
    ],
)
def test_saved_native_intervals_reject_invalid_values(rows: object) -> None:
    issuance = ForecastIssuance(native_intervals=rows)
    with pytest.raises(ValueError, match=r"Saved|saved"):
        issuance_power_intervals(issuance)


def test_database_rejects_nonfinite_factors_and_invalid_source_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = snapshot_for(system, DAY)
    item = prepare_forecast_issuances(source)[0]
    defaults = {
        "plant": plant,
        "configuration_id": item.configuration_id,
        "snapshot": source,
        "target_date": DAY,
        "published_at": source.collected_at,
        "cutoff_at": item.cutoff_at,
        "model_version": item.model_version,
        "factor": item.factor,
        "payloads": item.payloads,
        "outage_scenarios": item.outage_scenarios,
        "native_intervals": item.native_intervals,
    }
    for factor in (float("nan"), float("inf"), -1.0, 0.0, 0.5):
        with pytest.raises(IntegrityError), transaction.atomic():
            ForecastIssuance.objects.create(**(defaults | {"factor": factor}))
    source.source_kind = "estimated_actuals"
    stored_again(source)
    with pytest.raises(IntegrityError, match="lineage"), transaction.atomic():
        ForecastIssuance.objects.create(**defaults)


def test_database_requires_matching_calibration_and_pre_acquisition_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = snapshot_for(system, DAY)
    item = prepare_forecast_issuances(source)[0]
    run = ready_run(system, plant)
    defaults = {
        "plant": plant,
        "configuration_id": item.configuration_id,
        "snapshot": source,
        "calibration": run,
        "target_date": DAY,
        "published_at": source.collected_at,
        "cutoff_at": item.cutoff_at,
        "model_version": item.model_version,
        "factor": run.factor,
        "payloads": item.payloads,
        "outage_scenarios": item.outage_scenarios,
        "native_intervals": item.native_intervals,
    }
    with pytest.raises(IntegrityError, match="lineage"), transaction.atomic():
        ForecastIssuance.objects.create(**defaults)
    activation = CalibrationActivation.objects.create(
        plant=plant, configuration=run.configuration, calibration=run
    )
    with pytest.raises(IntegrityError, match="lineage"), transaction.atomic():
        ForecastIssuance.objects.create(**(defaults | {"factor": 0.75}))
    for field, value in (
        ("created_at", source.collected_at + timedelta(seconds=1)),
        ("cutoff_at", source.collected_at + timedelta(seconds=1)),
        ("completed_at", source.collected_at + timedelta(seconds=1)),
        ("status", "failed"),
    ):
        original = getattr(run, field)
        setattr(run, field, value)
        run.save(update_fields=(field,))
        with pytest.raises(IntegrityError, match="lineage"), transaction.atomic():
            ForecastIssuance.objects.create(**defaults)
        setattr(run, field, original)
        run.save(update_fields=(field,))
    activation.created_at = source.collected_at + timedelta(seconds=1)
    activation.save(update_fields=("created_at",))
    with pytest.raises(IntegrityError, match="lineage"), transaction.atomic():
        ForecastIssuance.objects.create(**defaults)
    activation.created_at = NOW
    activation.save(update_fields=("created_at",))
    issued = ForecastIssuance.objects.create(**defaults)
    assert issued.factor == run.factor
    assert issued.calibration_id == run.pk


def test_expired_location_purge_removes_issuance_before_protected_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = snapshot_for(system, DAY)
    freeze(source, plant)
    trash_region(system.member, system.tenant.pk, system.region.pk)
    assert purge_expired(NOW + RETENTION) == {"plants": 1, "regions": 1}
    assert not ForecastIssuance.objects.exists()
    assert not ForecastSnapshot.objects.filter(pk=source.pk).exists()
