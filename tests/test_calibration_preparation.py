from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier
from typing import Any, cast

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from pyracast.forecasts.history import HISTORY_SELECTION_POLICY
from pyracast.forecasts.models import ForecastSnapshot, HistoricalReferenceImport
from pyracast.irradiance import local_day_bounds
from pyracast.plants import measurements, training
from pyracast.plants.calibration_data import build_calibration_dataset
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    ProductionObservation,
)
from pyracast.plants.numerics import POLICY_VERSION, FitResult
from pyracast.plants.operations import (
    ObservationInput,
    activate_calibration,
    import_observations,
    request_calibration,
    reset_calibration,
)
from pyracast.plants.preparation import prepare_calibration
from pyracast.regions.models import Region
from pyracast.solar import daylight_window
from tests.test_domain import NOW, System
from tests.test_plant_operations import make_plant, ready_run
from tests.test_plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def add_days(
    system: System,
    plant: Plant,
    first: int,
    count: int,
    *,
    invalid: bool = False,
    daylight_only: bool = False,
) -> None:
    assert plant.current_configuration_id is not None
    rows = []
    for offset in range(first, first + count):
        day = date(2026, 11, offset)
        actual_snapshot_for(system, day)
        start, end = local_day_bounds(day, system.binding.timezone)
        cursor = start
        windows = daylight_window(
            day, system.binding.timezone, system.binding.latitude, system.binding.longitude
        ).windows
        while cursor < end:
            duration = timedelta(minutes=30 if offset % 2 else 60)
            selected = any(cursor < stop and cursor + duration > begin for begin, stop in windows)
            if daylight_only and not selected:
                cursor += duration
                continue
            rows.append(
                ObservationInput(
                    cursor,
                    cursor + duration,
                    26 * duration.total_seconds() / 3600,
                    "invalid" if invalid and cursor == start + timedelta(hours=12) else "valid",
                )
            )
            cursor += duration
    import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="MWh",
        observations=rows,
    )


def test_seven_daylight_only_days_schedule_and_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 7, daylight_only=True)
    run = CalibrationRun.objects.get()
    assert training.run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "ready"
    assert run.result["valid_days"] == 7
    assert run.result["metric_scope"] == "daylight_intervals"
    assert run.dataset_manifest["metric_scope"] == "daylight_intervals"
    assert len(run.dataset_manifest["required_windows"]) == 7
    assert len(run.dataset_manifest["observations"]) == ProductionObservation.objects.count()
    assert ProductionObservation.objects.count() < 7 * 24


@pytest.mark.parametrize("slot", ["dawn", "noon", "dusk"])
def test_daylight_meter_gap_does_not_prepare_a_complete_day(
    monkeypatch: pytest.MonkeyPatch, slot: str
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 7, daylight_only=True)
    CalibrationRun.objects.all().delete()
    start, end = local_day_bounds(date(2026, 11, 7), system.binding.timezone)
    observations = list(
        ProductionObservation.objects.filter(starts_at__gte=start, ends_at__lte=end).order_by(
            "starts_at"
        )
    )
    index = {"dawn": 0, "noon": len(observations) // 2, "dusk": -1}[slot]
    observations[index].delete()
    assert prepare_calibration(plant.pk) is None
    assert not CalibrationRun.objects.exists()


@pytest.mark.parametrize("invalid", [False, True])
def test_only_seven_complete_valid_matched_days_schedule(
    monkeypatch: pytest.MonkeyPatch, invalid: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 6)
    assert not CalibrationRun.objects.exists()
    add_days(system, plant, 7, 1, invalid=invalid)
    if invalid:
        assert not CalibrationRun.objects.exists()
        return
    run = CalibrationRun.objects.get()
    assert (run.status, run.window_start, run.window_end) == (
        "pending",
        date(2026, 11, 1),
        date(2026, 11, 7),
    )
    latest = ProductionObservation.objects.latest("pk")
    assert run.observation_id_cutoff == latest.pk
    assert prepare_calibration(plant.pk) is None
    assert not CalibrationActivation.objects.exists()


def test_preparation_pins_reset_and_refits_once_after_comparator_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    incumbent = ready_run(system, plant)
    active = activate_calibration(
        system.member, system.tenant.pk, system.region.pk, plant.pk, incumbent.pk
    )
    add_days(system, plant, 1, 7)
    run = CalibrationRun.objects.exclude(pk=incumbent.pk).get()
    assert run.comparison_activation_id == active.pk
    run.status = "rejected"
    run.result = {"policy_version": POLICY_VERSION}
    run.save(update_fields=("status", "result"))
    assert prepare_calibration(plant.pk) is None
    reset = reset_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk)
    next_run = prepare_calibration(plant.pk)
    assert next_run is not None
    assert next_run.comparison_activation_id == reset.pk
    next_run.status = "rejected"
    next_run.result = {"policy_version": POLICY_VERSION}
    next_run.save(update_fields=("status", "result"))
    assert prepare_calibration(plant.pk) is None


def test_activating_candidate_does_not_repeat_the_same_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 2, 7)
    run = ready_run(system, plant)
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)
    assert prepare_calibration(plant.pk) is None


@pytest.mark.parametrize("old_status", ["ready", "rejected", "insufficient"])
def test_legacy_policy_does_not_block_one_current_policy_fit(
    monkeypatch: pytest.MonkeyPatch, old_status: str
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 7)
    old = CalibrationRun.objects.get()
    old.status = old_status
    old.result = {"policy_version": "regularized-huber-v2"}
    old.save(update_fields=("status", "result"))
    current = prepare_calibration(plant.pk)
    assert current is not None
    assert current.pk != old.pk
    assert current.observation_id_cutoff == old.observation_id_cutoff
    assert training.run_calibration(current.pk, NOW)
    current.refresh_from_db()
    assert current.result["policy_version"] == POLICY_VERSION
    assert CalibrationRun.objects.count() == 2
    assert prepare_calibration(plant.pk) is None


@pytest.mark.parametrize("status", ["insufficient", "rejected", "failed"])
def test_current_policy_terminal_run_does_not_repeat_unchanged_data(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 7)
    run = CalibrationRun.objects.get()
    run.status = status
    run.result = {} if status == "failed" else {"policy_version": POLICY_VERSION}
    run.save(update_fields=("status", "result"))
    assert prepare_calibration(plant.pk) is None
    assert CalibrationRun.objects.count() == 1


@pytest.mark.parametrize("old_source", ["historical_import", "unknown_endpoint"])
def test_current_source_transition_refits_once_but_identical_native_reacquisition_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
    old_source: str,
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 7)
    CalibrationRun.objects.all().delete()
    target = date(2026, 11, 7)
    old_snapshot = ForecastSnapshot.objects.get(job__target_date=target)
    old_snapshot.collected_at = old_snapshot.available_at = NOW - timedelta(minutes=5)
    old_snapshot.save()
    if old_source == "historical_import":
        old_snapshot.job.reference_import = HistoricalReferenceImport.objects.create(
            binding=system.binding,
            sha256="a" * 64,
            source_name="authorized-export.json",
            source_settings={"product": "historical_rooftop_pv"},
            imported_by=system.admin,
            imported_at=old_snapshot.collected_at,
            interval_count=48,
        )
    old_snapshot.job.endpoint = old_source
    old_snapshot.job.save()
    historical_run = prepare_calibration(plant.pk)
    assert historical_run is not None
    historical_run.status = "rejected"
    historical_run.result = {"policy_version": POLICY_VERSION}
    historical_run.save()
    native = actual_snapshot_for(system, target, collected_at=NOW - timedelta(minutes=4))
    assert native.normalized_intervals == old_snapshot.normalized_intervals
    current_run = prepare_calibration(plant.pk)
    assert current_run is not None
    assert current_run.input_fingerprint != historical_run.input_fingerprint
    assert current_run.reference_snapshot_ids is not None
    assert current_run.reference_snapshot_ids[target.isoformat()] == native.pk
    current_run.status = "rejected"
    current_run.result = {"policy_version": POLICY_VERSION}
    current_run.save()
    reacquired = actual_snapshot_for(system, target, collected_at=NOW - timedelta(minutes=3))
    assert reacquired.normalized_intervals == native.normalized_intervals
    assert prepare_calibration(plant.pk) is None
    assert CalibrationRun.objects.count() == 2
    current_run.refresh_from_db()
    assert current_run.reference_snapshot_ids[target.isoformat()] == native.pk


def test_reference_selection_policy_upgrade_refits_unchanged_values_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    monkeypatch.setattr(
        measurements, "HISTORY_SELECTION_POLICY", "latest-complete-estimated-actuals-v2"
    )
    add_days(system, plant, 1, 7)
    previous = CalibrationRun.objects.get()
    previous.status = "rejected"
    previous.result = {"policy_version": POLICY_VERSION}
    previous.save()
    monkeypatch.setattr(measurements, "HISTORY_SELECTION_POLICY", HISTORY_SELECTION_POLICY)
    current = prepare_calibration(plant.pk)
    assert current is not None
    assert current.input_fingerprint != previous.input_fingerprint
    assert current.reference_snapshot_ids == previous.reference_snapshot_ids
    current.status = "rejected"
    current.result = {"policy_version": POLICY_VERSION}
    current.save()
    assert prepare_calibration(plant.pk) is None
    assert CalibrationRun.objects.count() == 2


def test_unavailable_configuration_or_reference_does_not_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    empty = Plant.objects.create(region=system.region, name="Unconfigured")
    assert prepare_calibration(empty.pk) is None
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    assert prepare_calibration(plant.pk) is None
    assert not CalibrationRun.objects.exists()


def test_missing_actual_history_does_not_count_as_a_matched_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 7)
    CalibrationRun.objects.all().delete()
    source = ForecastSnapshot.objects.order_by("pk").first()
    assert source is not None
    ForecastSnapshot.objects.filter(pk=source.pk).update(source_kind="forecast")
    assert prepare_calibration(plant.pk) is None
    assert not CalibrationRun.objects.exists()


def test_narrow_manual_run_does_not_suppress_wider_automatic_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 8)
    CalibrationRun.objects.all().delete()
    assert plant.current_configuration_id is not None
    manual = request_calibration(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 8),
    )
    assert training.run_calibration(manual.pk, NOW)
    automatic = CalibrationRun.objects.exclude(pk=manual.pk).get()
    assert (automatic.window_start, automatic.window_end) == (
        date(2026, 11, 1),
        date(2026, 11, 8),
    )
    assert automatic.status == "pending"


@pytest.mark.parametrize("running", [False, True])
def test_new_observations_during_active_run_are_queued_once_after_completion(
    monkeypatch: pytest.MonkeyPatch, running: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 3)
    add_days(system, plant, 5, 4)
    first = CalibrationRun.objects.get()
    lease = training._claim(first.pk, NOW) if running else None
    if running:
        assert lease is not None
    add_days(system, plant, 4, 1)
    new_ids = set(
        ProductionObservation.objects.filter(pk__gt=first.observation_id_cutoff).values_list(
            "pk", flat=True
        )
    )
    assert new_ids
    assert CalibrationRun.objects.count() == 1
    if lease is None:
        assert training.run_calibration(first.pk, NOW)
    else:
        dataset = build_calibration_dataset(lease.run, lease.configuration)
        assert dataset.samples
        assert training._publish(
            lease, FitResult("insufficient", None, "insufficient", {}), dataset.manifest, NOW
        )
    first.refresh_from_db()
    second = CalibrationRun.objects.exclude(pk=first.pk).get()
    assert second.status == "pending"
    assert second.observation_id_cutoff is not None
    assert first.observation_id_cutoff is not None
    assert second.observation_id_cutoff > first.observation_id_cutoff
    selected = {entry["observation_id"] for entry in first.dataset_manifest["observations"]}
    assert not selected.intersection(new_ids)
    assert max(selected) <= first.observation_id_cutoff
    assert all(row.recorded_at == first.cutoff_at for row in ProductionObservation.objects.all())
    assert training.run_calibration(second.pk, NOW + timedelta(seconds=1))
    second.refresh_from_db()
    selected = {entry["observation_id"] for entry in second.dataset_manifest["observations"]}
    assert new_ids <= selected
    assert CalibrationRun.objects.count() == 2
    assert not CalibrationActivation.objects.exists()


def test_concurrent_preparation_creates_one_durable_run(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 7)
    CalibrationRun.objects.all().delete()
    barrier = Barrier(2)

    def prepare() -> int | None:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            run = prepare_calibration(plant.pk)
            return None if run is None else run.pk
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: prepare(), range(2)))
    assert sum(value is not None for value in results) == 1
    assert CalibrationRun.objects.count() == 1


def test_legacy_run_without_watermark_preserves_timestamp_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 3)
    add_days(system, plant, 5, 4)
    run = CalibrationRun.objects.get()
    CalibrationRun.objects.filter(pk=run.pk).update(
        observation_id_cutoff=None, reference_snapshot_ids=None
    )
    add_days(system, plant, 4, 1)
    new_rows = ProductionObservation.objects.filter(pk__gt=run.observation_id_cutoff)
    first_new = new_rows.order_by("pk").first()
    last_new = new_rows.order_by("-pk").first()
    assert first_new is not None
    assert last_new is not None
    assert first_new.recorded_at == run.cutoff_at
    ProductionObservation.objects.filter(pk=last_new.pk).update(
        recorded_at=run.cutoff_at + timedelta(seconds=1)
    )
    lease = training._claim(run.pk, NOW)
    assert lease is not None
    dataset = build_calibration_dataset(lease.run, lease.configuration)
    entries = cast(list[dict[str, Any]], dataset.manifest["observations"])
    selected = {entry["observation_id"] for entry in entries}
    assert first_new.pk in selected
    assert last_new.pk not in selected


@pytest.mark.parametrize("terminal", ["retry", "exhausted"])
def test_failed_work_schedules_new_data_only_at_terminal_state(
    monkeypatch: pytest.MonkeyPatch, terminal: str
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 7)
    first = CalibrationRun.objects.get()
    add_days(system, plant, 8, 1)
    if terminal == "exhausted":
        CalibrationRun.objects.filter(pk=first.pk).update(attempts=3)
        assert not training.run_calibration(first.pk, NOW)
    else:

        def fail(*args: Any, **kwargs: Any) -> FitResult:
            raise ValueError("invalid")

        monkeypatch.setattr(training, "fit_calibration", fail)
        for attempt in range(3):
            assert not training.run_calibration(first.pk, NOW + timedelta(seconds=attempt))
            assert CalibrationRun.objects.count() == (1 if attempt < 2 else 2)
    first.refresh_from_db()
    assert first.status == "failed"
    assert CalibrationRun.objects.exclude(pk=first.pk).get().status == "pending"


def test_old_complete_history_uses_window_ending_at_latest_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 7)
    CalibrationRun.objects.all().delete()
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=120))
    run = prepare_calibration(plant.pk)
    assert run is not None
    assert run.window_start == date(2026, 11, 1)
    assert run.window_end == date(2026, 11, 7)


def test_automatic_pool_preserves_180_days_for_inner_window_comparisons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    for day in (date(2026, 5, 11), date(2026, 5, 12)):
        actual_snapshot_for(system, day)
        start, _ = local_day_bounds(day, system.binding.timezone)
        import_observations(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            configuration_id=plant.current_configuration_id,
            unit="MWh",
            observations=[
                ObservationInput(
                    start + timedelta(hours=hour), start + timedelta(hours=hour + 1), 26
                )
                for hour in range(24)
            ],
        )
    add_days(system, plant, 1, 7)
    run = CalibrationRun.objects.get()
    assert (run.window_start, run.window_end) == (date(2026, 5, 12), date(2026, 11, 7))
    assert run.reference_snapshot_ids is not None
    assert len(run.reference_snapshot_ids) == 8
    assert "2026-05-11" not in run.reference_snapshot_ids
