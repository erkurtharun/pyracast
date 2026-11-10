from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant
from pyracast.forecasts.models import CollectionJob, ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    ProductionObservation,
)
from pyracast.plants.numerics import POLICY_VERSION
from pyracast.plants.operations import (
    ObservationInput,
    activate_calibration,
    create_plant,
    import_observations,
    request_calibration,
    reset_calibration,
    revise_plant,
)
from pyracast.regions.models import Region
from tests.test_domain import NOW, System, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def make_plant(monkeypatch: pytest.MonkeyPatch) -> tuple[System, Plant]:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        name="Plant",
        dc_capacity_mw=130,
        export_limit_mw=100,
    )
    assert plant.current_configuration_id is not None
    PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(
        created_at=NOW - timedelta(days=100)
    )
    return system, plant


def snapshot_for(
    system: System,
    day: date,
    *,
    power_kw: float = 0.4,
    collected_at: datetime | None = None,
    issued_at: datetime | None = None,
    source_kind: str = "forecast",
) -> ForecastSnapshot:
    start, end = local_day_bounds(day, system.binding.timezone)
    collected_at = start - timedelta(hours=1) if collected_at is None else collected_at
    job = CollectionJob.objects.create(
        binding=system.binding,
        target_date=day,
        scheduled_slot=collected_at + timedelta(microseconds=CollectionJob.objects.count()),
        available_at=collected_at,
    )
    rows = []
    cursor = start
    while cursor < end:
        rows.append(
            {
                "start": cursor.isoformat(),
                "end": (cursor + timedelta(minutes=30)).isoformat(),
                "power_kw": power_kw,
            }
        )
        cursor += timedelta(minutes=30)
    return ForecastSnapshot.objects.create(
        job=job,
        raw_payload={},
        normalized_intervals=rows,
        collected_at=collected_at,
        issued_at=issued_at,
        source_kind=source_kind,
    )


def request_run(system: System, plant: Plant) -> CalibrationRun:
    assert plant.current_configuration_id is not None
    return request_calibration(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 8),
    )


def saved_actuals(system: System, day: date) -> ForecastSnapshot:
    start, end = local_day_bounds(day, system.binding.timezone)
    source = snapshot_for(
        system, day, collected_at=end + timedelta(hours=1), source_kind="estimated_actuals"
    )
    source.available_at = source.collected_at
    source.coverage_start, source.coverage_end = start, end
    source.save()
    source.job.status = "succeeded"
    source.job.save()
    return source


def ready_run(system: System, plant: Plant) -> CalibrationRun:
    run = request_run(system, plant)
    run.status, run.factor, run.assessment, run.completed_at = "ready", 0.5, "provisional", NOW
    run.result = {
        "policy_version": POLICY_VERSION,
        "holdout_days": 2,
        "baseline": {"daily_mae_mwh": 2},
        "candidate": {"daily_mae_mwh": 1},
    }
    run.dataset_manifest = {
        "selection_policy": "latest-available-estimated-actuals-v1",
        "source_kind": "estimated_actuals",
    }
    run.save()
    return run


@pytest.mark.parametrize("policy", ["regularized-huber-v1", "regularized-huber-v2"])
def test_older_policy_ready_candidate_cannot_be_activated(
    monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    system, plant = make_plant(monkeypatch)
    run = ready_run(system, plant)
    run.result["policy_version"] = policy
    run.save(update_fields=("result",))
    with pytest.raises(ValidationError):
        activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)
    assert not CalibrationActivation.objects.exists()


def test_ready_candidate_cannot_replace_a_changed_incumbent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = ready_run(system, plant)
    reset = reset_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk)
    with pytest.raises(ValidationError, match="changed"):
        activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)
    assert CalibrationActivation.objects.get().pk == reset.pk


def test_manual_request_pins_current_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    incumbent = ready_run(system, plant)
    activation = activate_calibration(
        system.member, system.tenant.pk, system.region.pk, plant.pk, incumbent.pk
    )
    run = request_run(system, plant)
    assert run.comparison_activation_id == activation.pk
    assert request_run(system, plant).comparison_activation_id == activation.pk


def test_concurrent_candidates_cannot_both_replace_the_same_incumbent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    candidates = (ready_run(system, plant), ready_run(system, plant))
    barrier = Barrier(2)

    def activate(run: CalibrationRun) -> int | None:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return activate_calibration(
                    system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk
                ).pk
            except ValidationError:
                return None
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        activations = list(pool.map(activate, candidates))
    assert sum(value is not None for value in activations) == 1
    assert CalibrationActivation.objects.count() == 1


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"selection_policy": "first-available-day-ahead-v1", "source_kind": "forecast"},
        {"selection_policy": "latest-available-estimated-actuals-v1", "source_kind": "forecast"},
    ],
)
def test_forecast_calibration_cannot_be_newly_activated(
    monkeypatch: pytest.MonkeyPatch, manifest: dict[str, str]
) -> None:
    system, plant = make_plant(monkeypatch)
    run = ready_run(system, plant)
    run.dataset_manifest = manifest
    run.save(update_fields=("dataset_manifest",))
    with pytest.raises(ValidationError):
        activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)
    assert not CalibrationActivation.objects.exists()


def test_configuration_revision_and_activation_history(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    first = PlantConfiguration.objects.get(pk=plant.current_configuration_id)
    run = ready_run(system, plant)
    activation = activate_calibration(
        system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk
    )
    assert (
        activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk).pk
        == activation.pk
    )
    reset = reset_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk)
    assert reset.calibration_id is None
    assert (
        reset_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk).pk
        == reset.pk
    )
    pending = request_run(system, plant)
    revised = revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name="New",
        dc_capacity_mw=140,
        inverter_capacity_mw=105,
        export_limit_mw=100,
    )
    pending.refresh_from_db()
    first.refresh_from_db()
    assert pending.status == "superseded"
    assert first.dc_capacity_mw == 130
    assert revised.current_configuration is not None
    assert revised.current_configuration.revision == 2
    assert CalibrationActivation.objects.count() == 2
    assert PlantEvent.objects.filter(action="configuration_created").count() == 2


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": " "},
        {"name": "x" * 201},
        {"dc_capacity_mw": True},
        {"dc_capacity_mw": float("nan")},
        {"dc_capacity_mw": 0},
        {"inverter_capacity_mw": float("inf")},
        {"export_limit_mw": -1},
        {"measurement_basis": "dc"},
        {"mounting_type": "dual_axis"},
        {"mounting_type": "unknown", "surface_tilt": 0},
        {"mounting_type": "tracking", "surface_azimuth": 180},
        {"mounting_type": "fixed", "surface_tilt": True},
        {"mounting_type": "fixed", "surface_tilt": float("nan")},
        {"mounting_type": "fixed", "surface_tilt": float("inf")},
        {"mounting_type": "fixed", "surface_tilt": -1},
        {"mounting_type": "fixed", "surface_tilt": 91},
        {"mounting_type": "fixed", "surface_azimuth": -1},
        {"mounting_type": "fixed", "surface_azimuth": 360},
    ],
)
def test_invalid_configuration_is_atomic(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]
) -> None:
    system, plant = make_plant(monkeypatch)
    values: dict[str, Any] = {"name": "Plant", "dc_capacity_mw": 130}
    values.update(arguments)
    with pytest.raises(ValidationError):
        create_plant(system.member, system.tenant.pk, system.region.pk, **values)
    with pytest.raises(ValidationError):
        revise_plant(system.member, system.tenant.pk, system.region.pk, plant.pk, **values)
    assert Plant.objects.count() == PlantConfiguration.objects.count() == 1


def test_geometry_revisions_preserve_explicit_unknowns_and_prior_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    initial = plant.current_configuration
    assert initial is not None
    assert (initial.mounting_type, initial.surface_tilt, initial.surface_azimuth) == (
        "unknown",
        None,
        None,
    )
    fixed = revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name="Fixed",
        dc_capacity_mw=130,
        export_limit_mw=100,
        mounting_type="fixed",
        surface_tilt=90,
        surface_azimuth=359.9,
    ).current_configuration
    assert fixed is not None
    assert (fixed.mounting_type, fixed.surface_tilt, fixed.surface_azimuth) == ("fixed", 90, 359.9)
    tracking = revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name="Tracking",
        dc_capacity_mw=130,
        export_limit_mw=100,
        mounting_type="tracking",
    ).current_configuration
    assert tracking is not None
    assert (tracking.mounting_type, tracking.surface_tilt, tracking.surface_azimuth) == (
        "tracking",
        None,
        None,
    )
    assert tracking.export_limit_mw == 100
    assert tracking.inverter_capacity_mw is None
    initial.refresh_from_db()
    fixed.refresh_from_db()
    assert initial.mounting_type == "unknown"
    assert (fixed.surface_tilt, fixed.surface_azimuth) == (90, 359.9)
    partial = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        name="Unknown fixed angles",
        dc_capacity_mw=1,
        mounting_type="fixed",
    ).current_configuration
    assert partial is not None
    assert partial.surface_tilt is None
    assert partial.surface_azimuth is None


def test_tenant_and_binding_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    stranger = User.objects.create(username="stranger")
    other = Tenant.objects.create(name="Other")
    Membership.objects.create(user=stranger, tenant=other)
    with pytest.raises(PermissionDenied):
        create_plant(stranger, system.tenant.pk, system.region.pk, name="X", dc_capacity_mw=1)
    with pytest.raises(Region.DoesNotExist):
        revise_plant(stranger, other.pk, system.region.pk, plant.pk, name="X", dc_capacity_mw=1)
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    with pytest.raises(ValidationError):
        create_plant(system.member, system.tenant.pk, system.region.pk, name="X", dc_capacity_mw=1)
    with pytest.raises(ValidationError):
        request_run(system, plant)
    Region.objects.filter(pk=system.region.pk).update(approved_binding=None)
    with pytest.raises(ValidationError):
        create_plant(system.member, system.tenant.pk, system.region.pk, name="X", dc_capacity_mw=1)


def test_observation_replay_units_zero_and_atomic_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    start = NOW - timedelta(days=2)
    saved_actuals(system, start.astimezone(ZoneInfo(system.binding.timezone)).date())
    item = ObservationInput(start, start + timedelta(hours=1), 0)
    arguments: dict[str, Any] = dict(
        configuration_id=plant.current_configuration_id, unit="kWh", observations=[item]
    )
    first = import_observations(
        system.member, system.tenant.pk, system.region.pk, plant.pk, **arguments
    )
    stamp = ProductionObservation.objects.get().recorded_at
    replay = import_observations(
        system.member, system.tenant.pk, system.region.pk, plant.pk, **arguments
    )
    assert first["created"] == replay["existing"] == 1
    assert ProductionObservation.objects.get().recorded_at == stamp
    second = ObservationInput(start + timedelta(hours=1), start + timedelta(hours=2), 500)
    conflict = ObservationInput(start + timedelta(minutes=30), start + timedelta(minutes=60), 1)
    with pytest.raises(ValidationError):
        import_observations(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            configuration_id=plant.current_configuration_id,
            unit="kWh",
            observations=[second, conflict],
        )
    assert ProductionObservation.objects.count() == 1
    import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="kWh",
        observations=[second],
    )
    assert ProductionObservation.objects.latest("starts_at").energy_mwh == 0.5


def test_new_configuration_accepts_old_meter_history_and_future_batch_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="New", dc_capacity_mw=130
    )
    configuration = plant.current_configuration
    assert configuration is not None
    assert configuration.created_at == NOW
    start = NOW - timedelta(days=7)
    saved_actuals(system, start.astimezone(ZoneInfo(system.binding.timezone)).date())
    past = ObservationInput(start, start + timedelta(hours=1), 26)
    future = ObservationInput(NOW, NOW + timedelta(hours=1), 26)
    with pytest.raises(ValidationError, match="Completed"):
        import_observations(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            configuration_id=configuration.pk,
            unit="MWh",
            observations=[past, future],
        )
    assert not ProductionObservation.objects.exists()
    assert not PlantEvent.objects.filter(action="observations_imported").exists()
    result = import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=configuration.pk,
        unit="MWh",
        observations=[past],
    )
    assert result["created"] == 1
    observation = ProductionObservation.objects.get()
    assert observation.ends_at < configuration.created_at
    assert observation.recorded_at == NOW


@pytest.mark.parametrize(
    "case",
    [
        "unit",
        "empty",
        "large",
        "naive",
        "duration",
        "future",
        "alignment",
        "midnight",
        "negative",
        "bool",
        "infinite",
        "quality",
        "source",
        "long_source",
    ],
)
def test_invalid_observations(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    start = NOW - timedelta(days=1)
    end, value, quality, source, unit = start + timedelta(hours=1), 0.0, "valid", "operator", "MWh"
    if case == "naive":
        start = start.replace(tzinfo=None)
    elif case == "duration":
        end += timedelta(minutes=30)
    elif case == "future":
        start, end = NOW, NOW + timedelta(hours=1)
    elif case == "alignment":
        start, end = start + timedelta(minutes=1), end + timedelta(minutes=1)
    elif case == "midnight":
        start = datetime(2026, 11, 7, 20, 30, tzinfo=UTC)
        end = start + timedelta(hours=1)
    elif case in {"negative", "bool", "infinite"}:
        value = {"negative": -1.0, "bool": True, "infinite": float("inf")}[case]
    elif case == "quality":
        quality = "untrusted"
    elif case in {"source", "long_source"}:
        source = " " if case == "source" else "x" * 201
    elif case == "unit":
        unit = "MW"
    records = [ObservationInput(start, end, value, quality, source)]
    if case == "empty":
        records = []
    elif case == "large":
        records *= 2001
    with pytest.raises(ValidationError):
        import_observations(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            configuration_id=plant.current_configuration_id,
            unit=unit,
            observations=records,
        )
    assert not ProductionObservation.objects.exists()


def test_calibration_request_bounds_dedup_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    assert request_run(system, plant).pk == run.pk
    assert plant.current_configuration_id is not None
    for config_id, start, end in [
        (0, date(2026, 11, 2), date(2026, 11, 8)),
        (plant.current_configuration_id, date(2026, 11, 2), date(2026, 11, 9)),
        (plant.current_configuration_id, date(2026, 11, 5), date(2026, 11, 8)),
        (plant.current_configuration_id, date(2026, 11, 1), date(2026, 11, 8)),
    ]:
        with pytest.raises(ValidationError):
            request_calibration(
                system.member,
                system.tenant.pk,
                system.region.pk,
                plant.pk,
                configuration_id=config_id,
                start_date=start,
                end_date=end,
            )
    with pytest.raises(ValidationError):
        activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)


def test_concurrent_observation_replay_is_one_row(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    saved_actuals(system, (NOW - timedelta(days=1)).date())
    barrier = Barrier(2)
    configuration_id = plant.current_configuration_id
    assert configuration_id is not None

    def upload() -> dict[str, object]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return import_observations(
                system.member,
                system.tenant.pk,
                system.region.pk,
                plant.pk,
                configuration_id=configuration_id,
                unit="MWh",
                observations=[
                    ObservationInput(NOW - timedelta(days=1), NOW - timedelta(hours=23), 0)
                ],
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: upload(), range(2)))
    assert sorted(cast(int, result["created"]) for result in results) == [0, 1]
    assert ProductionObservation.objects.count() == 1


def test_configless_plant_cannot_activate_and_historical_imports_keep_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    unfinished = Plant.objects.create(region=system.region, name="Unfinished")
    with pytest.raises(ValidationError, match="configuration_unavailable"):
        reset_calibration(system.member, system.tenant.pk, system.region.pk, unfinished.pk)
    old_id = plant.current_configuration_id
    assert old_id is not None
    observation = ObservationInput(NOW - timedelta(days=1), NOW - timedelta(hours=23), 0)
    saved_actuals(system, (NOW - timedelta(days=1)).date())
    import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=old_id,
        unit="MWh",
        observations=[observation],
    )
    revised = revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name="Revised",
        dc_capacity_mw=140,
    )
    import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=old_id,
        unit="MWh",
        observations=[observation],
    )
    saved = ProductionObservation.objects.get()
    assert saved.configuration_id == old_id
    assert saved.recorded_at == NOW
    assert revised.current_configuration_id is not None
    with pytest.raises(ValidationError, match="Conflicting"):
        import_observations(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            configuration_id=revised.current_configuration_id,
            unit="MWh",
            observations=[observation],
        )


def test_concurrent_calibration_requests_share_one_durable_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    barrier = Barrier(2)

    def enqueue() -> int:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return request_run(system, plant).pk
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: enqueue(), range(2)))
    assert len(set(ids)) == 1
    assert CalibrationRun.objects.count() == 1
