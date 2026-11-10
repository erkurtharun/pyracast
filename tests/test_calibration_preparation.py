from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier
from typing import Any, cast

import pytest
from django.db import close_old_connections, connection

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants import training
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    ProductionObservation,
)
from pyracast.plants.numerics import FitResult
from pyracast.plants.operations import ObservationInput, import_observations, request_calibration
from pyracast.plants.preparation import prepare_calibration
from pyracast.regions.models import Region
from tests.test_domain import NOW, System
from tests.test_plant_operations import make_plant
from tests.test_plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def add_days(
    system: System, plant: Plant, first: int, count: int, *, invalid: bool = False
) -> None:
    assert plant.current_configuration_id is not None
    rows = []
    for offset in range(first, first + count):
        day = date(2026, 11, offset)
        actual_snapshot_for(system, day)
        start, end = local_day_bounds(day, system.binding.timezone)
        cursor = start
        while cursor < end:
            # Alternate half-hour and hourly days to exercise mixed resolutions.
            duration = timedelta(minutes=30 if offset % 2 else 60)
            rows.append(
                ObservationInput(
                    cursor,
                    cursor + duration,
                    26 * duration.total_seconds() / 3600,
                    "invalid" if invalid and cursor == start else "valid",
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
        samples, manifest, _ = training._dataset(lease)
        assert samples
        assert training._publish(
            lease, FitResult("insufficient", None, "insufficient", {}), manifest, NOW
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
    CalibrationRun.objects.filter(pk=run.pk).update(observation_id_cutoff=None)
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
    _, manifest, _ = training._dataset(lease)
    entries = cast(list[dict[str, Any]], manifest["observations"])
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
