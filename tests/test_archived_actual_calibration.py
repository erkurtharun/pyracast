from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any, cast

import pytest

from pyracast.forecasts.actuals import ACTUAL_COMPOSITION_POLICY, ACTUAL_PIN_FORMAT
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.availability import reference_intervals
from pyracast.plants.calibration_data import build_calibration_dataset, current_source_validation
from pyracast.plants.measurements import select_measurements, selection_fingerprint
from pyracast.plants.models import CalibrationRun, Plant
from pyracast.plants.numerics import POLICY_VERSION
from pyracast.plants.operations import activate_calibration
from pyracast.plants.performance import forecast_performance
from pyracast.plants.preparation import prepare_calibration
from pyracast.plants.production_changes import ObservationInput, import_observations
from pyracast.plants.training import run_calibration
from tests.support.domain import NOW, System
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant, snapshot_for
from tests.support.plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 8)


def actual_piece(
    system: System,
    day: date,
    first_hour: int,
    last_hour: int,
    *,
    power_kw: float = 0.4,
    acquired: datetime | None = None,
) -> ForecastSnapshot:
    start, _ = local_day_bounds(day, system.binding.timezone)
    when = acquired or start + timedelta(hours=last_hour, minutes=5)
    source = snapshot_for(
        system, day, power_kw=power_kw, collected_at=when, source_kind="estimated_actuals"
    )
    source.normalized_intervals = source.normalized_intervals[first_hour * 2 : last_hour * 2]
    source.coverage_start = start + timedelta(hours=first_hour)
    source.coverage_end = start + timedelta(hours=last_hour)
    source.available_at = when
    stored_again(source)
    source.job.endpoint, source.job.status = "estimated_actuals", "succeeded"
    source.job.save(update_fields=("endpoint", "status"))
    return source


def import_day(system: System, plant: Plant, day: date, *, end_hour: int = 18) -> None:
    start, _ = local_day_bounds(day, system.binding.timezone)
    assert plant.current_configuration_id is not None
    import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="MWh",
        observations=[
            ObservationInput(start + timedelta(hours=hour), start + timedelta(hours=hour + 1), 26)
            for hour in range(7, end_hour)
        ],
    )


def test_calibration_pins_disjoint_actual_acquisitions_and_ignores_later_same_timestamp_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    morning = actual_piece(system, DAY, 7, 12)
    afternoon = actual_piece(system, DAY, 12, 18)
    import_day(system, plant, DAY)
    run = CalibrationRun.objects.get(plant=plant)
    assert plant.current_configuration is not None
    assert run.reference_snapshot_ids is not None
    pinned = deepcopy(run.reference_snapshot_ids)
    pin = pinned[DAY.isoformat()]
    assert pin["format"] == ACTUAL_PIN_FORMAT
    assert pin["snapshot_ids"] == [afternoon.pk, morning.pk]
    original = build_calibration_dataset(run, plant.current_configuration)
    source = cast(dict[str, Any], original.manifest["sources"])[DAY.isoformat()]
    assert original.manifest["selection_policy"] == ACTUAL_COMPOSITION_POLICY
    assert source["source_snapshot_ids"] == [afternoon.pk, morning.pk]
    assert len(original.samples) == 11
    assert sum(sample.energy_mwh for sample in original.samples) == 286
    assert sum(
        piece.power_mw * (piece.end - piece.start).total_seconds() / 3600
        for sample in original.samples
        for piece in sample.intervals
        if piece.power_mw is not None
    ) == pytest.approx(572)
    correction = actual_piece(system, DAY, 7, 18, power_kw=0.8, acquired=run.cutoff_at)
    repeated = build_calibration_dataset(run, plant.current_configuration)
    assert repeated.manifest == original.manifest
    assert repeated.samples == original.samples
    assert correction.pk not in source["source_snapshot_ids"]
    run.refresh_from_db()
    assert run.reference_snapshot_ids == pinned
    assert plant.current_configuration is not None
    current = reference_intervals(plant.current_configuration, DAY, run.cutoff_at)
    assert all(row.power_mw == pytest.approx(104) for row in current)


def test_identical_composite_reacquisition_deduplicates_values_while_preserving_pinned_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    actual_piece(system, DAY, 7, 12)
    actual_piece(system, DAY, 12, 18)
    import_day(system, plant, DAY)
    run = CalibrationRun.objects.get(plant=plant)
    assert plant.current_configuration is not None
    start, end = local_day_bounds(DAY, system.binding.timezone)
    rows, _ = select_measurements(plant.current_configuration, start, end, NOW)
    before, old_ids = selection_fingerprint(plant.current_configuration, rows, NOW)
    replacement = actual_piece(system, DAY, 7, 18, acquired=NOW)
    after, new_ids = selection_fingerprint(plant.current_configuration, rows, NOW)
    assert after == before
    assert old_ids != new_ids
    assert cast(dict[str, Any], new_ids[DAY.isoformat()])["snapshot_ids"] == [replacement.pk]
    assert run.reference_snapshot_ids == old_ids


def test_full_native_and_composite_representations_share_effective_day_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    full = actual_snapshot_for(system, DAY)
    import_day(system, plant, DAY)
    run = CalibrationRun.objects.get(plant=plant)
    run.status, run.completed_at = "insufficient", NOW
    run.result = {"policy_version": POLICY_VERSION}
    run.save(update_fields=("status", "completed_at", "result"))
    assert plant.current_configuration is not None
    start, end = local_day_bounds(DAY, system.binding.timezone)
    rows, _ = select_measurements(plant.current_configuration, start, end, NOW)
    original, pins = selection_fingerprint(plant.current_configuration, rows, NOW)
    assert pins[DAY.isoformat()] == full.pk
    actual_piece(system, DAY, 0, 12, acquired=NOW - timedelta(minutes=2))
    actual_piece(system, DAY, 12, 24, acquired=NOW - timedelta(minutes=1))
    composed, new_pins = selection_fingerprint(plant.current_configuration, rows, NOW)
    assert composed == original
    assert isinstance(new_pins[DAY.isoformat()], dict)
    assert prepare_calibration(plant.pk, NOW) is None
    replacement = actual_snapshot_for(system, DAY, collected_at=NOW)
    repeated, repeated_pins = selection_fingerprint(plant.current_configuration, rows, NOW)
    assert repeated == original
    assert repeated_pins[DAY.isoformat()] == replacement.pk
    replacement.normalized_intervals.append(
        {
            "start": end.isoformat(),
            "end": (end + timedelta(minutes=30)).isoformat(),
            "power_kw": 0.9,
        }
    )
    replacement.coverage_end = end + timedelta(minutes=30)
    stored_again(replacement)
    unrelated, _ = selection_fingerprint(plant.current_configuration, rows, NOW)
    assert unrelated == original
    assert prepare_calibration(plant.pk, NOW) is None
    run.refresh_from_db()
    assert run.reference_snapshot_ids == pins
    assert CalibrationRun.objects.count() == 1


def test_historical_model_comparison_uses_the_same_archived_actual_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    morning = actual_piece(system, DAY, 7, 12)
    afternoon = actual_piece(system, DAY, 12, 18)
    import_day(system, plant, DAY)
    result = forecast_performance(
        plant, start_date=DAY, end_date=DAY, comparison="historical_model", daylight_only=True
    )
    assert result["matched_days"] == 1
    row = result["days"][0]
    assert row["predicted_energy_mwh"] == pytest.approx(572)
    assert row["measured_energy_mwh"] == pytest.approx(286)
    assert row["source_snapshot_ids"] == [afternoon.pk, morning.pk]
    assert row["reference_provenance"]["selection_policy"] == ACTUAL_COMPOSITION_POLICY


def test_seven_composite_native_days_fit_without_synthetic_provider_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source_ids = []
    for offset in range(7):
        day = date(2026, 11, 2) + timedelta(days=offset)
        source_ids.extend(
            [actual_piece(system, day, 7, 12).pk, actual_piece(system, day, 12, 19).pk]
        )
        import_day(system, plant, day, end_hour=19)
    run = CalibrationRun.objects.get(plant=plant)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "insufficient"
    run = CalibrationRun.objects.get(plant=plant, status="pending")
    assert run_calibration(run.pk, NOW + timedelta(minutes=1))
    run.refresh_from_db()
    assert run.status == "ready", run.result
    assert run.assessment == "provisional"
    assert run.factor is not None
    assert 0.5 < run.factor < 0.6
    assert ForecastSnapshot.objects.filter(source_kind="estimated_actuals").count() == 14
    manifests = cast(dict[str, dict[str, Any]], run.dataset_manifest["sources"])
    assert {
        identifier for source in manifests.values() for identifier in source["source_snapshot_ids"]
    } <= set(source_ids)
    monkeypatch.setattr(
        "pyracast.plants.operations.timezone.now", lambda: NOW + timedelta(minutes=2)
    )
    activation = activate_calibration(
        system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk
    )
    assert activation.calibration_id == run.pk


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "bool_id",
        "duplicate_id",
        "primary",
        "member",
        "member_bool_id",
        "binding",
        "forecast",
        "unknown_product",
        "normalized",
        "imported",
        "length",
        "members",
        "identifiers",
        "empty",
    ],
)
def test_native_composition_promotion_support_requires_every_owned_native_source(
    damage: str | None,
) -> None:
    days = [(date(2026, 10, 1) + timedelta(days=offset)).isoformat() for offset in range(30)]
    member = {
        "snapshot_id": 1,
        "binding_id": 1,
        "source_kind": "estimated_actuals",
        "source_product": "rooftop_pv_power",
        "reference_import": None,
        "capacity_normalization": None,
    }
    members: list[Any] = [member, member | {"snapshot_id": 2}]
    source = member | {
        "selection_policy": ACTUAL_COMPOSITION_POLICY,
        "source_snapshot_ids": [1, 2],
        "source_snapshots": members,
    }
    if damage == "bool_id":
        source["source_snapshot_ids"] = [True, 2]
    elif damage == "duplicate_id":
        source["source_snapshot_ids"] = [1, 1]
    elif damage == "primary":
        source["snapshot_id"] = 3
    elif damage == "member":
        members[1] = None
    elif damage == "member_bool_id":
        members[0]["snapshot_id"] = True
    elif damage == "binding":
        members[1]["binding_id"] = True
    elif damage == "forecast":
        members[1]["source_kind"] = "forecast"
    elif damage == "unknown_product":
        members[1]["source_product"] = "unknown"
    elif damage == "normalized":
        members[1]["capacity_normalization"] = {"id": 1}
    elif damage == "imported":
        members[1]["reference_import"] = {"id": 1}
    elif damage == "length":
        members.pop()
    elif damage == "members":
        source["source_snapshots"] = None
    elif damage == "identifiers":
        source["source_snapshot_ids"] = None
    elif damage == "empty":
        source["source_snapshot_ids"] = []
    report = {
        "valid_days": 30,
        "included_day_dates": days,
        "training_days": 23,
        "training_day_dates": days[:-7],
        "holdout_days": 7,
        "holdout_day_dates": days[-7:],
    }
    result = current_source_validation(
        {"binding_id": 1, "sources": dict.fromkeys(days, source)}, report, 1
    )
    assert result["eligible"] is (damage is None)
    assert result["native_valid_days"] == (30 if damage is None else 0)
