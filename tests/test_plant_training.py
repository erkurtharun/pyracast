import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.utils import timezone

from pyracast.forecasts.history import HISTORY_SELECTION_POLICY
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.plants import training
from pyracast.plants.activation import activate_calibration, reset_calibration
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    PlantEvent,
    ProductionObservation,
)
from pyracast.plants.numerics import FitResult, fit_calibration
from pyracast.plants.operations import revise_plant
from pyracast.plants.production_changes import ObservationInput, import_observations
from pyracast.plants.training import LEASE_DURATION, pending_calibration_ids, run_calibration
from pyracast.regions.models import Region
from pyracast.solar import DAYLIGHT_POLICY
from tests.support.domain import NOW
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant, ready_run, request_run, snapshot_for
from tests.support.plant_training import actual_snapshot_for, history

pytestmark = pytest.mark.django_db(transaction=True)


def test_durable_fit_freezes_inputs_and_requires_separate_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    sources = history(system, plant)
    run = request_run(system, plant)
    assert pending_calibration_ids(NOW) == [run.pk]
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "ready"
    assert run.assessment == "provisional"
    assert run.factor is not None
    assert 0.5 < run.factor < 0.6
    assert len(run.dataset_sha256) == 64
    assert run.dataset_manifest["daylight_policy"] == DAYLIGHT_POLICY
    assert run.dataset_manifest["metric_scope"] == "daylight_intervals"
    assert run.dataset_manifest["coverage_policy"] == "whole-meter-intervals-cover-daylight-v1"
    assert sorted(run.dataset_manifest["required_windows"]) == [
        (date(2026, 11, 2) + timedelta(days=offset)).isoformat() for offset in range(7)
    ]
    assert run.result["exclusions"]["outside_required_windows"] > 0
    assert len(run.dataset_manifest["observations"]) == 168
    assert (
        sorted(value["snapshot_id"] for value in run.dataset_manifest["sources"].values())
        == sources
    )
    assert run.result["selection"]["unmatched_days"] == []
    assert not CalibrationActivation.objects.exists()
    digest = run.dataset_sha256
    assert not run_calibration(run.pk, NOW + timedelta(minutes=1))
    run.refresh_from_db()
    assert run.dataset_sha256 == digest
    assert run.attempts == 1
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(minutes=1))
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)
    assert CalibrationActivation.objects.get().calibration_id == run.pk
    assert not pending_calibration_ids(NOW)


def test_worker_compares_against_the_activation_at_request_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    incumbent = ready_run(system, plant)
    incumbent.factor = 0.8
    incumbent.save(update_fields=("factor",))
    activation = activate_calibration(
        system.member, system.tenant.pk, system.region.pk, plant.pk, incumbent.pk
    )
    history(system, plant)
    run = request_run(system, plant)
    reset_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.dataset_manifest["comparison_activation_id"] == activation.pk
    assert run.dataset_manifest["incumbent_factor"] == 0.8
    assert run.result["comparison_activation_id"] == activation.pk
    assert run.result["incumbent_factor"] == 0.8
    assert run.result["incumbent"]
    with pytest.raises(ValidationError, match="changed"):
        activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)


def test_worker_uses_identity_for_pinned_reset_and_retries_invalid_incumbent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    reset = reset_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk)
    history(system, plant)
    run = request_run(system, plant)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.result["incumbent_factor"] == 1.0
    assert run.result["comparison_activation_id"] == reset.pk
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(minutes=1))
    active = activate_calibration(
        system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk
    )
    next_run = request_run(system, plant)
    run.factor = None
    run.save(update_fields=("factor",))
    assert next_run.comparison_activation_id == active.pk
    assert not run_calibration(next_run.pk, NOW + timedelta(minutes=1))
    next_run.refresh_from_db()
    assert next_run.status == "pending"
    assert next_run.last_error == "invalid_calibration_data"


def test_negative_power_outside_matched_days_does_not_break_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    sources = history(system, plant)
    source = ForecastSnapshot.objects.get(pk=sources[0])
    first_start = datetime.fromisoformat(source.normalized_intervals[0]["start"])
    source.normalized_intervals.insert(
        0,
        {
            "start": (first_start - timedelta(minutes=30)).isoformat(),
            "end": first_start.isoformat(),
            "power_kw": -1,
        },
    )
    source.coverage_start = first_start - timedelta(minutes=30)
    stored_again(source)
    run = request_run(system, plant)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "ready"
    assert run.result["valid_days"] == 7
    assert len(run.dataset_manifest["observations"]) == 168
    assert run.factor is not None
    assert 0.5 < run.factor < 0.6


def test_archived_forecasts_cannot_replace_missing_estimated_actuals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    sources = history(system, plant)
    for source in ForecastSnapshot.objects.filter(pk__in=sources):
        stored_again(source, source_kind="forecast")
    CalibrationRun.objects.all().delete()
    run = request_run(system, plant)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "insufficient"
    assert run.factor is None
    assert len(run.result["selection"]["unmatched_days"]) == 7
    assert run.result["selection"]["unmatched_observations"] == 168
    assert len(run.dataset_sha256) == 64


def test_actuals_and_meter_cutoff_exclude_later_information(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    sources = history(system, plant)
    CalibrationRun.objects.all().delete()
    first = ForecastSnapshot.objects.get(pk=sources[0])
    second = ForecastSnapshot.objects.get(pk=sources[1])
    stored_again(ForecastSnapshot.objects.get(pk=first.pk), collected_at=NOW + timedelta(seconds=1))
    stored_again(ForecastSnapshot.objects.get(pk=second.pk), issued_at=NOW + timedelta(seconds=1))
    excluded = ProductionObservation.objects.order_by("starts_at").last()
    assert excluded is not None
    excluded.delete()
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(seconds=1))
    excluded.save()
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    run = request_run(system, plant)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "insufficient"
    assert run.result["selection"]["recorded_after_cutoff"] == 1
    assert run.result["selection"]["unmatched_observations"] == 48
    ids = [entry["observation_id"] for entry in run.dataset_manifest["observations"]]
    assert excluded.pk not in ids


def test_empty_history_is_audited_insufficient(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "insufficient"
    assert run.dataset_manifest["observations"] == []
    assert (
        PlantEvent.objects.filter(action="calibration_completed").get().details["status"]
        == "insufficient"
    )


def test_lease_recovery_duplicate_and_old_owner_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    first = training._claim(run.pk, NOW)
    assert first is not None
    assert not run_calibration(run.pk, NOW)
    assert not pending_calibration_ids(NOW)
    expired = NOW + LEASE_DURATION + timedelta(seconds=1)
    assert pending_calibration_ids(expired) == [run.pk]
    result = FitResult("insufficient", None, "insufficient", {})
    assert not training._publish(first, result, {}, expired)
    assert run_calibration(run.pk, expired)
    assert not training._publish(first, result, {}, expired)
    training._fail(first, expired)
    run.refresh_from_db()
    assert run.status == "insufficient"
    assert run.attempts == 2


def test_worker_revalidates_binding_and_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    assert not run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "superseded"


@pytest.mark.parametrize("change", ["revise", "suspend"])
def test_revision_or_approval_change_during_fit_cannot_publish(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    original = fit_calibration

    def changed(*args: Any, **kwargs: Any) -> FitResult:
        assert not connection.in_atomic_block
        if change == "revise":
            revise_plant(
                system.member,
                system.tenant.pk,
                system.region.pk,
                plant.pk,
                name="Changed",
                dc_capacity_mw=140,
            )
        else:
            Region.objects.filter(pk=system.region.pk).update(status="suspended")
        return original(*args, **kwargs)

    monkeypatch.setattr(training, "fit_calibration", changed)
    assert not run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "superseded"
    assert run.factor is None
    assert not run.dataset_sha256


def test_source_errors_retry_bounded_and_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)

    def fail(*args: Any, **kwargs: Any) -> FitResult:
        raise ValueError("sensitive malformed provider payload")

    monkeypatch.setattr(training, "fit_calibration", fail)
    for attempt in range(3):
        assert not run_calibration(run.pk, NOW + timedelta(seconds=attempt))
    run.refresh_from_db()
    assert run.status == "failed"
    assert run.attempts == 3
    assert run.last_error == "invalid_calibration_data"
    assert not run_calibration(run.pk, NOW + timedelta(minutes=1))


@pytest.mark.parametrize("case", ["future_cutoff", "exhausted", "expired_lease"])
def test_invalid_or_interrupted_run_state(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    if case == "future_cutoff":
        run.cutoff_at = NOW + LEASE_DURATION + timedelta(seconds=1)
    elif case == "exhausted":
        run.attempts = 3
    else:
        run.status, run.lease_token, run.lease_expires_at = "running", uuid.uuid4(), NOW
    run.save()
    assert pending_calibration_ids(NOW) == [run.pk]
    assert run_calibration(run.pk, NOW) == (case == "expired_lease")
    run.refresh_from_db()
    assert run.status == ("insufficient" if case == "expired_lease" else "failed")


def test_worker_clock_and_dispatch_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    with pytest.raises(ValidationError):
        run_calibration(run.pk, datetime(2026, 11, 9))
    for now, limit in [(datetime(2026, 11, 9), 1), (NOW, 0), (NOW, 1001), (NOW, True)]:
        with pytest.raises(ValidationError):
            pending_calibration_ids(now, limit)
    assert run_calibration(run.pk)


def test_meter_native_intervals_are_frozen_before_hourly_fitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = actual_snapshot_for(system, date(2026, 11, 8))
    source.normalized_intervals[0]["power_kw"] = 120 / 130
    source.normalized_intervals[1]["power_kw"] = 80 / 130
    stored_again(source)
    start = datetime.fromisoformat(source.normalized_intervals[0]["start"])
    assert plant.current_configuration_id is not None
    import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="MWh",
        observations=[ObservationInput(start, start + timedelta(hours=1), 90)],
    )
    run = request_run(system, plant)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    records = cast(list[dict[str, Any]], run.dataset_manifest["observations"])
    assert [row["power_mw"] for row in records[0]["intervals"]] == pytest.approx([120, 80])


def test_publication_and_failure_include_elapsed_lock_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    lease = training._claim(run.pk, NOW)
    assert lease is not None
    ticks = iter([0.0, LEASE_DURATION.total_seconds() + 1])
    monkeypatch.setattr(training, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert not training._publish(
        lease, FitResult("insufficient", None, "insufficient", {}), {}, NOW
    )
    ticks = iter([0.0, LEASE_DURATION.total_seconds() + 1])
    training._fail(lease, NOW)
    run.refresh_from_db()
    assert run.status == "running"
    assert not run.dataset_sha256


def test_training_uses_latest_actuals_available_at_request_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    sources = history(system, plant)
    intervals = []
    for offset, source_id in enumerate(sources):
        day = date(2026, 11, 2) + timedelta(days=offset)
        source = ForecastSnapshot.objects.get(pk=source_id)
        intervals.extend(source.normalized_intervals)
        snapshot_for(system, day, power_kw=0.9)
    latest = actual_snapshot_for(system, date(2026, 11, 9), collected_at=NOW - timedelta(hours=1))
    latest.normalized_intervals = intervals
    latest.coverage_start = datetime.fromisoformat(intervals[0]["start"])
    latest.coverage_end = datetime.fromisoformat(intervals[-1]["end"])
    latest.issued_at = latest.collected_at
    stored_again(latest)
    future = actual_snapshot_for(
        system, date(2026, 11, 10), collected_at=NOW + timedelta(seconds=1)
    )
    future.normalized_intervals = [dict(row, power_kw=0.9) for row in intervals]
    future.coverage_start, future.coverage_end = latest.coverage_start, latest.coverage_end
    stored_again(future)
    CalibrationRun.objects.all().delete()
    run = request_run(system, plant)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "ready"
    assert run.dataset_manifest["selection_policy"] == HISTORY_SELECTION_POLICY
    assert run.dataset_manifest["source_kind"] == "estimated_actuals"
    assert (
        sorted(value["snapshot_id"] for value in run.dataset_manifest["sources"].values())
        == [latest.pk] * 7
    )
    assert all(
        value["source_kind"] == "estimated_actuals"
        for value in run.dataset_manifest["sources"].values()
    )


def test_concurrent_delivery_only_publishes_one_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    fitting, release = Event(), Event()

    def blocked_fit(*args: Any, **kwargs: Any) -> FitResult:
        fitting.set()
        assert release.wait(timeout=10)
        return fit_calibration(*args, **kwargs)

    monkeypatch.setattr(training, "fit_calibration", blocked_fit)

    def execute() -> bool:
        close_old_connections()
        try:
            return run_calibration(run.pk, NOW)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute)
        try:
            assert fitting.wait(timeout=10)
            assert pool.submit(execute).result(timeout=10) is False
        finally:
            release.set()
        assert first.result(timeout=10)
    run.refresh_from_db()
    assert run.attempts == 1
    assert run.status == "insufficient"
    assert PlantEvent.objects.filter(action="calibration_completed").count() == 1


def test_worker_failure_logs_type_and_origin_not_message(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)

    def fail(*args: Any, **kwargs: Any) -> FitResult:
        raise ValueError("sensitive malformed provider payload")

    monkeypatch.setattr(training, "fit_calibration", fail)
    assert not run_calibration(run.pk, NOW)
    [message] = [
        record.getMessage()
        for record in pyracast_logs.records
        if record.getMessage().startswith("calibration_attempt_failed")
    ]
    assert message.startswith(
        f"calibration_attempt_failed run={run.pk} attempt=1 error_type=ValueError "
        "origin=pyracast.plants.training:"
    )
    assert message.endswith(":run_calibration")
    assert "sensitive" not in pyracast_logs.text


def test_lost_lease_is_logged(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    lease = training._claim(run.pk, NOW)
    assert lease is not None
    expired = NOW + LEASE_DURATION + timedelta(seconds=1)
    assert not training._publish(
        lease, FitResult("insufficient", None, "insufficient", {}), {}, expired
    )
    training._fail(lease, expired)
    assert [
        record.getMessage()
        for record in pyracast_logs.records
        if record.getMessage().startswith("calibration_lease_lost")
    ] == [f"calibration_lease_lost run={run.pk}"] * 2


@pytest.mark.parametrize(("attempts", "reason"), [(3, "attempt_limit"), (0, "invalid_cutoff")])
def test_terminal_claim_failure_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    pyracast_logs: pytest.LogCaptureFixture,
    attempts: int,
    reason: str,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    run.attempts = attempts
    if reason == "invalid_cutoff":
        run.cutoff_at = NOW + timedelta(hours=1)
    run.save()
    assert not run_calibration(run.pk, NOW)
    assert f"calibration_run_failed run={run.pk} reason={reason}" in [
        record.getMessage() for record in pyracast_logs.records
    ]


def test_fit_completion_logs_duration(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    run_calibration(run.pk, NOW)
    [message] = [
        record.getMessage()
        for record in pyracast_logs.records
        if record.getMessage().startswith("calibration_fit_completed")
    ]
    assert re.fullmatch(rf"calibration_fit_completed run={run.pk} seconds=\d+\.\d{{3}}", message)
