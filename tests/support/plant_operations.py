from datetime import date, datetime, timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from pyracast.forecasts.history import HISTORY_SELECTION_POLICY
from pyracast.forecasts.models import CollectionJob, ForecastResult, ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import (
    CalibrationRun,
    Plant,
    PlantConfiguration,
)
from pyracast.plants.numerics import POLICY_VERSION
from pyracast.plants.operations import (
    create_plant,
    request_calibration,
)
from tests.support.domain import NOW, System, make_system


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
        binding=system.binding,
        raw_payload={},
        normalized_intervals=rows,
        collected_at=collected_at,
        issued_at=issued_at,
        source_kind=source_kind,
    )


def publish_snapshot(
    snapshot: ForecastSnapshot, *, published_at: datetime | None = None
) -> ForecastSnapshot:
    from pyracast.calculation import calculate
    from pyracast.forecasts.projections import prepare_daily_payloads
    from pyracast.forecasts.sources import snapshot_data
    from pyracast.plants.forecast_archive import (
        prepare_forecast_issuances,
        save_forecast_issuances,
    )

    snapshot.coverage_start = datetime.fromisoformat(snapshot.normalized_intervals[0]["start"])
    snapshot.coverage_end = datetime.fromisoformat(snapshot.normalized_intervals[-1]["end"])
    published_at = published_at or snapshot.collected_at
    snapshot.available_at = published_at
    snapshot.save()
    prepared = prepare_forecast_issuances(snapshot)
    data = snapshot_data(snapshot)
    payload = calculate(snapshot.binding, snapshot.job.target_date, data)
    daily_payloads = prepare_daily_payloads(
        snapshot.binding, snapshot.job.target_date, data, calculate, payload
    )
    with transaction.atomic():
        plant = (
            Plant.objects.select_for_update().filter(region_id=snapshot.binding.region_id).first()
        )
        snapshot.job.status = "succeeded"
        snapshot.job.save(update_fields=["status"])
        ForecastResult.objects.create(
            snapshot=snapshot,
            payload=payload,
            daily_payloads=daily_payloads,
            published_at=published_at,
        )
        save_forecast_issuances(prepared, snapshot, published_at, plant=plant)
    return snapshot


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
        "selection_policy": HISTORY_SELECTION_POLICY,
        "source_kind": "estimated_actuals",
    }
    run.save()
    return run
