from datetime import date, timedelta

import pytest

from pyracast.plants import training
from pyracast.plants.automation import set_calibration_automation
from pyracast.plants.models import CalibrationRun, Plant, PlantEvent
from pyracast.plants.operations import request_calibration, revise_plant
from pyracast.plants.preparation import prepare_calibration
from tests.support.calibration_preparation import add_days
from tests.support.concurrency import behind_open_transaction
from tests.support.domain import NOW
from tests.support.plant_operations import make_plant, request_run

pytestmark = pytest.mark.django_db(transaction=True)


def completion(run: CalibrationRun) -> dict[str, object]:
    event = PlantEvent.objects.get(action="calibration_completed", details__calibration_id=run.pk)
    return dict(event.details)


def test_a_run_superseded_at_claim_records_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    monkeypatch.setattr(training, "production_identity_current", lambda _: False)

    assert training.run_calibration(run.pk, NOW) is False

    run.refresh_from_db()
    assert (run.status, run.lease_token) == ("superseded", None)
    assert run.completed_at is not None
    assert completion(run) == {
        "calibration_id": run.pk,
        "status": "superseded",
        "reason": "production_source_changed",
        "dataset_sha256": "",
    }


def test_a_run_failed_at_its_attempt_limit_records_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    CalibrationRun.objects.filter(pk=run.pk).update(attempts=training.MAX_ATTEMPTS)

    assert training.run_calibration(run.pk, NOW) is False

    run.refresh_from_db()
    assert (run.status, run.last_error) == ("failed", "attempt_limit")
    assert completion(run)["reason"] == "attempt_limit"


def test_a_configuration_revision_supersedes_open_runs_and_disables_automation_with_causes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    set_calibration_automation(*scope, enabled=True)
    run = request_run(system, plant)

    revise_plant(*scope, name="Changed", dc_capacity_mw=140)

    run.refresh_from_db()
    assert (run.status, run.completed_at, run.lease_token) == ("superseded", NOW, None)
    assert completion(run)["reason"] == "configuration_changed"
    disabled = PlantEvent.objects.get(action="calibration_automation_disabled")
    plant.refresh_from_db()
    assert disabled.details == {
        "configuration_id": plant.current_configuration_id,
        "cause": "configuration_revised",
    }


@pytest.mark.parametrize(
    ("ahead", "status"), [(timedelta(minutes=1), "pending"), (timedelta(hours=1), "failed")]
)
def test_a_cutoff_ahead_of_the_worker_clock_waits_one_lease_before_failing(
    monkeypatch: pytest.MonkeyPatch, ahead: timedelta, status: str
) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)
    CalibrationRun.objects.filter(pk=run.pk).update(cutoff_at=NOW + ahead)

    assert training.run_calibration(run.pk, NOW) is False

    run.refresh_from_db()
    assert run.status == status
    assert run.attempts == 0


@pytest.mark.parametrize(
    ("reason", "age", "retried"),
    [
        ("attempt_limit", timedelta(days=1, minutes=1), True),
        ("invalid_cutoff", timedelta(days=1, minutes=1), True),
        ("attempt_limit", timedelta(hours=1), False),
        ("invalid_calibration_data", timedelta(days=3), False),
    ],
)
def test_preparation_retries_a_run_that_failed_without_a_data_error_once_a_day(
    monkeypatch: pytest.MonkeyPatch, reason: str, age: timedelta, retried: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 7)
    failed = CalibrationRun.objects.get()
    CalibrationRun.objects.filter(pk=failed.pk).update(
        status="failed", last_error=reason, completed_at=NOW - age
    )

    run = prepare_calibration(plant.pk, NOW)

    assert (run is not None) is retried
    if run is not None:
        assert run.input_sha256 == failed.input_sha256


def test_concurrent_workers_claim_a_run_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    run = request_run(system, plant)

    def claim() -> bool:
        return training._claim(run.pk, NOW) is not None

    assert behind_open_transaction(claim, claim) == (True, False)
    run.refresh_from_db()
    assert (run.status, run.attempts) == ("running", 1)


def test_requested_and_prepared_runs_pin_the_same_inputs_for_one_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 7)
    prepared = CalibrationRun.objects.get()
    CalibrationRun.objects.filter(pk=prepared.pk).update(status="insufficient", completed_at=NOW)
    assert plant.current_configuration_id is not None

    requested = request_calibration(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 7),
    )

    pinned = (
        "configuration_id",
        "binding_id",
        "comparison_activation_id",
        "window_start",
        "window_end",
        "cutoff_at",
        "observation_id_cutoff",
        "production_snapshot_ids",
        "measurement_selection_policy",
        "reference_snapshot_ids",
        "incident_revision_ids",
        "input_sha256",
    )
    assert {name: getattr(requested, name) for name in pinned} == {
        name: getattr(prepared, name) for name in pinned
    }
    assert Plant.objects.get(pk=plant.pk).current_configuration_id == requested.configuration_id
