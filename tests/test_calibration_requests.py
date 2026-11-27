from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from django.db import connection
from dramatiq.brokers.stub import StubBroker

from pyracast.background.failures import ItemsFailedError
from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.secrets import encrypt_secret
from pyracast.forecasts.jobs.claim import claim
from pyracast.forecasts.jobs.collect import persist_snapshot
from pyracast.forecasts.models import CollectionJob, HistoricalReferenceImport
from pyracast.incidents.models import PlantIncident
from pyracast.intervals import NormalizedInterval
from pyracast.irradiance import local_day_bounds
from pyracast.orchestration.kinds import CALIBRATION
from pyracast.plants import automation, preparation, training
from pyracast.plants.automation import set_calibration_automation
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantEvent,
    ProductionObservation,
)
from pyracast.plants.production_changes import ObservationInput, import_observations
from pyracast.plants.runtime import dispatch_calibrations
from tests.support.calibration_preparation import add_days
from tests.support.concurrency import behind_open_transaction
from tests.support.domain import NOW, System
from tests.support.historical_reference_import import archive, power_file
from tests.support.incident_calibration import incident
from tests.support.jobs import publish
from tests.support.plant_operations import half_hours, make_plant
from tests.support.plant_training import actual_snapshot_for
from tests.support.production_changes import edit, write

pytestmark = pytest.mark.django_db(transaction=True)


class Crash(BaseException):
    pass


def raise_private(*args: object, **kwargs: object) -> None:
    raise RuntimeError("private preparation detail")


def write_observations(system: System, plant: Plant, _: Path) -> None:
    add_days(system, plant, 1, 1)


def enable_automation(system: System, plant: Plant, _: Path) -> None:
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )


def declare_outage(system: System, plant: Plant, _: Path) -> None:
    incident(system, plant, NOW - timedelta(days=2), NOW - timedelta(days=1))


def change_production(system: System, plant: Plant, _: Path) -> None:
    start, _end = local_day_bounds(NOW.date() - timedelta(days=2), system.binding.timezone)
    write(system, plant, [edit(start, 2)])


def publish_estimated_actuals(system: System, plant: Plant, _: Path) -> None:
    moment = NOW - timedelta(seconds=1)
    day = NOW.date() - timedelta(days=1)
    start, _end = local_day_bounds(day, system.binding.timezone)
    job = CollectionJob.objects.create(
        binding=system.binding,
        target_date=day,
        endpoint="estimated_actuals",
        scheduled_for=moment,
        available_at=moment,
    )
    lease = claim(job.pk, moment)
    assert lease is not None
    data = ProviderData(
        {"fixture": "estimated_actuals"},
        NormalizedInterval.from_rows(half_hours(start, start + timedelta(days=1), 0.4)),
        collected_at=moment,
        source_kind="estimated_actuals",
    )
    snapshot = persist_snapshot(lease, data, moment)
    assert snapshot is not None
    assert publish(lease, snapshot, {}, moment)


def import_history(system: System, plant: Plant, directory: Path) -> None:
    ProviderAccount.objects.filter(pk=system.account.pk).update(
        provider="solcast", encrypted_api_key=encrypt_secret("test-only-solcast-key")
    )
    SiteBinding.objects.filter(pk=system.binding.pk).update(
        dc_capacity_kw=1, settings_verification_reference="Verified test model settings"
    )
    archive(system, power_file(directory))


WRITERS: dict[str, tuple[Callable[[System, Plant, Path], None], Callable[[Plant], bool]]] = {
    "observations": (write_observations, lambda plant: ProductionObservation.objects.exists()),
    "automation": (
        enable_automation,
        lambda plant: Plant.objects.get(pk=plant.pk).automatic_calibration_enabled,
    ),
    "outage": (declare_outage, lambda plant: PlantIncident.objects.filter(plant=plant).exists()),
    "production_change": (
        change_production,
        lambda plant: ProductionObservation.objects.filter(plant=plant).exists(),
    ),
    "estimated_actuals": (
        publish_estimated_actuals,
        lambda plant: CollectionJob.objects.filter(
            endpoint="estimated_actuals", status="succeeded"
        ).exists(),
    ),
    "historical_import": (
        import_history,
        lambda plant: HistoricalReferenceImport.objects.exists(),
    ),
}


def committed_request(plant_id: int) -> datetime | None:
    with (
        psycopg.connect(**connection.get_connection_params(), autocommit=True) as other,
        other.cursor() as cursor,
    ):
        cursor.execute("SET TIME ZONE 'UTC'")
        cursor.execute(
            "SELECT calibration_preparation_due_at FROM plants_plant WHERE id = %s", (plant_id,)
        )
        row = cursor.fetchone()
    assert row is not None
    value: datetime | None = row[0]
    return value


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_each_writer_commits_while_preparation_raises(
    monkeypatch: pytest.MonkeyPatch,
    pyracast_logs: pytest.LogCaptureFixture,
    tmp_path: Path,
    writer: str,
) -> None:
    system, plant = make_plant(monkeypatch)
    perform, persisted = WRITERS[writer]
    monkeypatch.setattr(preparation, "latest_measurement_day", raise_private)

    perform(system, plant, tmp_path)

    assert persisted(plant)
    retry_at = committed_request(plant.pk)
    assert retry_at is not None
    assert retry_at > NOW
    assert f"calibration_preparation_failed item=plant:{plant.pk}" in pyracast_logs.text
    assert "private preparation detail" not in pyracast_logs.text


@pytest.mark.parametrize("step", ["promotion", "preparation"])
def test_a_finished_calibration_persists_while_its_follow_up_raises(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture, step: str
) -> None:
    system, plant = make_plant(monkeypatch)
    add_days(system, plant, 1, 7, daylight_only=True)
    run = CalibrationRun.objects.get()
    Plant.objects.filter(pk=plant.pk).update(
        automatic_calibration_enabled=True,
        automatic_calibration_enabled_by=system.member,
        next_calibration_check_at=NOW + timedelta(days=1),
    )
    if step == "promotion":
        monkeypatch.setattr(
            "pyracast.plants.automation.promote_automatic_calibration", raise_private
        )
    else:
        monkeypatch.setattr(preparation, "latest_measurement_day", raise_private)

    assert training.run_calibration(run.pk, NOW)

    run.refresh_from_db()
    assert run.status == "ready"
    assert run.last_error == ""
    if step == "promotion":
        assert (
            automatic_decisions(plant)
            == [("calibration_automatic_skipped", "promotion_failed")] * 2
        )
    else:
        assert committed_request(plant.pk) is not None
    assert "private preparation detail" not in pyracast_logs.text


def crash_after_write(monkeypatch: pytest.MonkeyPatch, system: System, plant: Plant) -> None:
    def crash(*args: object, **kwargs: object) -> None:
        raise Crash

    with monkeypatch.context() as patched:
        patched.setattr(preparation, "latest_measurement_day", crash)
        with pytest.raises(Crash):
            write_observations(system, plant, Path())


def test_a_crash_between_the_write_and_its_preparation_loses_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    crash_after_write(monkeypatch, system, plant)

    assert ProductionObservation.objects.filter(plant=plant).exists()
    assert committed_request(plant.pk) == NOW
    assert not CalibrationRun.objects.exists()

    broker = StubBroker()
    assert dispatch_calibrations(CALIBRATION.make_actor(broker)) == 1
    run = CalibrationRun.objects.get()
    assert run.status == "pending"
    assert committed_request(plant.pk) is None


def test_concurrent_dispatchers_prepare_one_request_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    crash_after_write(monkeypatch, system, plant)
    actor = CALIBRATION.make_actor(StubBroker())
    original = preparation.prepare_calibration
    attempts: list[int] = []

    def counted(plant_id: int, now: datetime | None = None) -> CalibrationRun | None:
        attempts.append(plant_id)
        return original(plant_id, now)

    monkeypatch.setattr(preparation, "prepare_calibration", counted)

    first, second = behind_open_transaction(
        lambda: dispatch_calibrations(actor), lambda: dispatch_calibrations(actor)
    )

    assert attempts == [plant.pk]
    assert (first, second) == (1, 1), "the second dispatcher waited for the first one's commit"
    assert CalibrationRun.objects.count() == 1
    assert committed_request(plant.pk) is None


def automated_candidate(system: System, plant: Plant) -> CalibrationRun:
    enable_automation(system, plant, Path())
    rows: list[ObservationInput] = []
    for offset in range(30):
        day = date(2026, 10, 9) + timedelta(days=offset)
        actual_snapshot_for(system, day)
        start, end = local_day_bounds(day, system.region.timezone)
        while start < end:
            rows.append(ObservationInput(start, start + timedelta(hours=1), 26))
            start += timedelta(hours=1)
    assert plant.current_configuration_id is not None
    import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="MWh",
        observations=rows,
    )
    return CalibrationRun.objects.get()


def failing_promotions(monkeypatch: pytest.MonkeyPatch, failures: int) -> list[int]:
    original = automation.promote_automatic_calibration
    calls: list[int] = []

    def fail_first(
        plant: Plant, candidate: CalibrationRun, now: datetime, enabler: automation.Enabler
    ) -> bool:
        calls.append(candidate.pk)
        if len(calls) <= failures:
            raise RuntimeError("private promotion detail")
        return original(plant, candidate, now, enabler)

    monkeypatch.setattr(automation, "promote_automatic_calibration", fail_first)
    return calls


def automatic_decisions(plant: Plant) -> list[tuple[str, str | None]]:
    return list(
        PlantEvent.objects.filter(
            plant=plant,
            action__in=("calibration_automatic_skipped", "calibration_automatically_activated"),
        )
        .order_by("pk")
        .values_list("action", "details__reason")
    )


def test_a_failed_automatic_promotion_is_retried_without_a_new_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = automated_candidate(system, plant)
    calls = failing_promotions(monkeypatch, 1)

    assert training.run_calibration(run.pk, NOW)

    assert calls == [run.pk, run.pk]
    assert CalibrationActivation.objects.get().calibration_id == run.pk
    assert CalibrationRun.objects.count() == 1
    assert automatic_decisions(plant) == [
        ("calibration_automatic_skipped", "promotion_failed"),
        ("calibration_automatically_activated", None),
    ]
    assert committed_request(plant.pk) is None


def test_a_failed_promotion_is_retried_after_an_unrelated_daily_check_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = automated_candidate(system, plant)
    calls = failing_promotions(monkeypatch, 2)

    assert training.run_calibration(run.pk, NOW)

    retry_at = committed_request(plant.pk)
    assert calls == [run.pk, run.pk]
    assert retry_at is not None
    assert retry_at >= NOW + preparation.PREPARATION_RETRY_DELAY
    assert preparation.prepare_requested_calibrations(retry_at - timedelta(minutes=1)) == []
    assert calls == [run.pk, run.pk], "the request's back-off bounds the retries"
    Plant.objects.filter(pk=plant.pk).update(next_calibration_check_at=retry_at)
    with monkeypatch.context() as failing:
        failing.setattr(automation, "prepare_calibration", raise_private)
        with pytest.raises(ItemsFailedError):
            automation.prepare_due_calibrations(retry_at)

    assert preparation.prepare_requested_calibrations(retry_at) == []

    assert calls == [run.pk, run.pk, run.pk]
    assert CalibrationActivation.objects.get().calibration_id == run.pk
    assert CalibrationRun.objects.count() == 1
    assert automatic_decisions(plant) == [
        ("calibration_automatic_skipped", "promotion_failed"),
        ("calibration_automatic_skipped", "promotion_failed"),
        ("calibration_automatic_skipped", "preparation_failed"),
        ("calibration_automatically_activated", None),
    ]
    assert committed_request(plant.pk) is None


def test_a_promotion_that_keeps_raising_stops_after_three_recorded_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = automated_candidate(system, plant)
    calls = failing_promotions(monkeypatch, 99)

    assert training.run_calibration(run.pk, NOW)
    retry_at = committed_request(plant.pk)
    assert retry_at is not None
    assert preparation.prepare_requested_calibrations(retry_at) == []

    assert calls == [run.pk] * 3
    assert committed_request(plant.pk) is None
    assert preparation.prepare_requested_calibrations(retry_at + timedelta(days=1)) == []
    assert calls == [run.pk] * 3
    assert automatic_decisions(plant) == [("calibration_automatic_skipped", "promotion_failed")] * 3
    assert not CalibrationActivation.objects.exists()


def test_a_request_is_prepared_only_while_it_is_due(monkeypatch: pytest.MonkeyPatch) -> None:
    _, plant = make_plant(monkeypatch)
    assert preparation._prepare_requested(plant.pk + 1000, NOW) is None
    assert preparation._prepare_requested(plant.pk, NOW) is None
    later = NOW + timedelta(hours=1)
    Plant.objects.filter(pk=plant.pk).update(calibration_preparation_due_at=later)
    assert preparation._prepare_requested(plant.pk, NOW) is None
    assert committed_request(plant.pk) == later
    assert not CalibrationRun.objects.exists()
