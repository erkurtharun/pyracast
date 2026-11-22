import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from hashlib import sha256
from io import StringIO
from pathlib import Path
from threading import Barrier
from typing import Any

import psycopg
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import (
    IntegrityError,
    OperationalError,
    close_old_connections,
    connection,
    transaction,
)
from psycopg.errors import LockNotAvailable

from pyracast.accounts.models import Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.coverage import history_day_complete
from pyracast.forecasts.historical_upload import import_historical_upload, parse_historical_upload
from pyracast.forecasts.history import select_actual_snapshot
from pyracast.forecasts.management.commands import import_historical_reference as command
from pyracast.forecasts.models import CollectionJob, ForecastSnapshot, HistoricalReferenceImport
from pyracast.forecasts.trust import PROBLEM_MESSAGES, reference_problems
from pyracast.irradiance import local_day_bounds
from pyracast.irradiance.solcast_export import load_json_file
from pyracast.plants.operations import create_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.models import AuditEvent, Region
from tests.support.domain import NOW
from tests.support.historical_reference_import import (
    REFERENCE,
    archive,
    power_file,
    system_for_import,
)
from tests.support.invariants import stored_again
from tests.support.plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def test_archive_preserves_instants_missingness_and_actual_import_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    path = power_file(tmp_path)
    attempts = system.account.requestattempt_set.count()
    record = archive(system, path)
    assert record.created_at == NOW
    assert record.imported_by == system.admin
    assert record.source_name == path.name
    assert record.content_sha256 == sha256(path.read_bytes()).hexdigest()
    assert record.interval_count == 3
    assert record.source_settings["product"] == "historical_rooftop_pv"
    assert record.source_settings["source_reference"] == REFERENCE
    assert record.source_settings["settings_matched"] is True
    snapshots = list(ForecastSnapshot.objects.filter(job__reference_import=record).order_by("pk"))
    assert len(snapshots) == 2
    first, second = snapshots
    assert first.job.target_date == date(2026, 11, 7)
    assert second.job.target_date == date(2026, 11, 8)
    assert first.normalized_intervals == [
        {"start": "2026-11-07T20:00:00+00:00", "end": "2026-11-07T20:30:00+00:00", "power_kw": 0},
        {"start": "2026-11-07T20:30:00+00:00", "end": "2026-11-07T21:00:00+00:00", "power_kw": 0.5},
    ]
    assert second.normalized_intervals == [
        {"start": "2026-11-07T21:30:00+00:00", "end": "2026-11-07T22:00:00+00:00", "power_kw": 0},
    ]
    for snapshot in snapshots:
        assert snapshot.source_kind == "estimated_actuals"
        assert snapshot.collected_at == snapshot.available_at == snapshot.job.scheduled_for == NOW
        assert snapshot.issued_at is None
        assert snapshot.job.created_at == NOW
        assert snapshot.job.available_at == NOW
        assert snapshot.job.status == "succeeded"
        assert snapshot.job.endpoint == "historical_import"
        assert snapshot.job.attempts == 0
        assert snapshot.raw_payload["historical_import"] == {
            "id": record.pk,
            "sha256": record.content_sha256,
        }
        assert not history_day_complete(snapshot, snapshot.job.target_date, system.binding.timezone)
    assert first.raw_payload["estimated_actuals"][0]["period_end"] == "2026-11-07T22:30:00+02:00"
    assert system.account.requestattempt_set.count() == attempts
    assert (
        AuditEvent.objects.filter(
            region=system.region, action="historical_reference_imported"
        ).count()
        == 1
    )
    assert (
        select_actual_snapshot(system.binding, date(2026, 11, 7), NOW - timedelta(seconds=1))
        is None
    )
    assert select_actual_snapshot(system.binding, date(2026, 11, 7), NOW) == first


def test_idempotence_and_corrections_keep_immutable_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    path = power_file(tmp_path)
    original = archive(system, path)
    original_snapshot = ForecastSnapshot.objects.filter(job__reference_import=original).first()
    assert original_snapshot is not None
    assert archive(system, path) == original
    assert archive(system, path, reference="Changed declaration") == original
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["estimated_actuals"][0]["pv_power_rooftop"] = 0.2
    path.write_text(json.dumps(payload), encoding="utf-8")
    corrected = archive(system, path)
    assert corrected != original
    assert corrected.content_sha256 != original.content_sha256
    assert HistoricalReferenceImport.objects.count() == 2
    original_snapshot.refresh_from_db()
    assert original_snapshot.normalized_intervals[0]["power_kw"] == 0
    assert CollectionJob.objects.filter(reference_import=original).count() == 2
    assert CollectionJob.objects.filter(reference_import=corrected).count() == 2


@pytest.mark.parametrize("native_complete", [True, False])
@pytest.mark.parametrize("import_complete", [True, False])
def test_import_supplements_native_history_without_downgrading_complete_current_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_complete: bool,
    import_complete: bool,
) -> None:
    system = system_for_import(monkeypatch)
    day = date(2026, 11, 7)
    native = actual_snapshot_for(system, day, power_kw=0.4, collected_at=NOW - timedelta(hours=2))
    if not native_complete:
        native.normalized_intervals = native.normalized_intervals[6:]
        stored_again(native)
    start, _ = local_day_bounds(day, system.binding.timezone)
    rows = [
        {
            "period_end": (start + timedelta(minutes=30 * (index + 1))).isoformat(),
            "period": "PT30M",
            "pv_power_rooftop": 0.7,
        }
        for index in range(48)
    ]
    imported = archive(system, power_file(tmp_path, rows if import_complete else rows[6:]))
    historical = ForecastSnapshot.objects.get(job__reference_import=imported)
    expected = native if native_complete or not import_complete else historical
    selected = select_actual_snapshot(system.binding, day, NOW)
    assert selected == expected
    assert selected is not None
    assert selected.normalized_intervals == expected.normalized_intervals
    assert select_actual_snapshot(system.binding, day, NOW - timedelta(seconds=1)) == native


def test_current_native_vintages_keep_latest_available_complete_acquisition_and_cutoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    day = date(2026, 11, 7)
    first = actual_snapshot_for(system, day, power_kw=0.4, collected_at=NOW - timedelta(hours=2))
    second = actual_snapshot_for(system, day, power_kw=0.5, collected_at=NOW - timedelta(hours=1))
    start, _ = local_day_bounds(day, system.binding.timezone)
    rows = [
        {
            "period_end": (start + timedelta(minutes=30 * (index + 1))).isoformat(),
            "period": "PT30M",
            "pv_power_rooftop": 0.7,
        }
        for index in range(48)
    ]
    archive(system, power_file(tmp_path, rows))
    assert select_actual_snapshot(system.binding, day, NOW) == second
    assert select_actual_snapshot(system.binding, day, NOW - timedelta(minutes=90)) == first
    second.normalized_intervals = second.normalized_intervals[6:]
    stored_again(second)
    assert select_actual_snapshot(system.binding, day, NOW) == first


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("latitude", 41),
        ("longitude", 28),
        ("capacity_kw", 2),
        ("dc_capacity_kw", 2),
        ("azimuth_degrees", 1),
        ("losses_percent", 1),
    ],
)
def test_import_never_rescales_mismatched_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: object,
) -> None:
    system = system_for_import(monkeypatch)
    metadata: dict[str, Any] = {key: value}
    with pytest.raises(ValidationError, match="conflicts with the selected reference"):
        archive(system, power_file(tmp_path), **metadata)
    assert not HistoricalReferenceImport.objects.exists()
    assert not ForecastSnapshot.objects.exists()


@pytest.mark.parametrize(
    ("model", "changes", "problem"),
    [
        ("account", {"enabled": False}, "binding_not_current"),
        ("account", {"authorized_at": None}, "binding_not_current"),
        ("account", {"authorized_by_id": None}, "account_unattested"),
        ("account", {"authorization_reference": ""}, "account_unattested"),
        ("account", {"provider": "fixture"}, "account_not_solcast"),
        ("account", {"credential_revision": 2}, "binding_not_current"),
        (
            "binding",
            {"validated_at": None, "validated_credential_revision": None},
            "binding_not_validated",
        ),
        ("binding", {"capacity_semantics_verified": False}, "capacity_unverified"),
        ("binding", {"settings_verification_reference": ""}, "settings_unverified"),
        ("region", {"status": "suspended"}, "binding_not_current"),
        ("region", {"revision": 2}, "binding_not_current"),
    ],
)
def test_import_requires_current_authorized_validated_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    changes: dict[str, Any],
    problem: str,
) -> None:
    system = system_for_import(monkeypatch)
    instance = getattr(system, model)
    type(instance).objects.filter(pk=instance.pk).update(**changes)
    with pytest.raises(ValidationError) as refusal:
        archive(system, power_file(tmp_path))
    assert str(PROBLEM_MESSAGES[problem]) in refusal.value.messages
    assert not HistoricalReferenceImport.objects.exists()


def test_dc_power_basis_cannot_authorize_historical_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    binding = SiteBinding.objects.select_related("provider_account", "region").get(
        pk=system.binding.pk
    )
    binding.power_basis = "dc"
    assert reference_problems(binding) == ("power_basis_not_ac",)
    assert not HistoricalReferenceImport.objects.exists()


def test_admin_and_tenant_region_binding_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    path = power_file(tmp_path)
    upload = parse_historical_upload(path.read_bytes(), path.name)

    def run(actor: Any, tenant_id: int, binding_id: int = system.binding.pk) -> object:
        return import_historical_upload(
            actor,
            tenant_id,
            system.region.pk,
            binding_id,
            upload,
            path.name,
            settings_confirmed=True,
        )

    with pytest.raises(PermissionDenied):
        run(system.member, system.tenant.pk)
    other = Tenant.objects.create(name="Other")
    with pytest.raises(Region.DoesNotExist):
        run(system.admin, other.pk)
    Region.objects.filter(pk=system.region.pk).update(deleted_at=NOW)
    with pytest.raises(Region.DoesNotExist):
        run(system.admin, system.tenant.pk)
    Region.objects.filter(pk=system.region.pk).update(deleted_at=None)
    with pytest.raises(SiteBinding.DoesNotExist):
        run(system.admin, system.tenant.pk, system.binding.pk + 100)
    assert not HistoricalReferenceImport.objects.exists()


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [
            {"period_end": "2026-11-09T15:30:00+02:00", "period": "PT30M", "pv_power_rooftop": 0},
        ],
    ],
)
def test_empty_and_future_intervals_are_not_archived(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: object,
) -> None:
    system = system_for_import(monkeypatch)
    with pytest.raises(ValidationError, match=r"between 1|future"):
        archive(system, power_file(tmp_path, rows))
    assert not HistoricalReferenceImport.objects.exists()


def test_native_interval_crossing_local_midnight_is_preserved_in_both_day_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    path = power_file(
        tmp_path,
        [
            {
                "period_end": "2026-11-08T00:15:00+03:00",
                "period": "PT30M",
                "pv_power_rooftop": 0.5,
            }
        ],
    )
    record = archive(system, path)
    snapshots = list(ForecastSnapshot.objects.filter(job__reference_import=record))
    assert len(snapshots) == 2
    assert {item.job.target_date for item in snapshots} == {date(2026, 11, 7), date(2026, 11, 8)}
    assert snapshots[0].normalized_intervals == snapshots[1].normalized_intervals
    assert snapshots[0].normalized_intervals == [
        {
            "start": "2026-11-07T20:45:00+00:00",
            "end": "2026-11-07T21:15:00+00:00",
            "power_kw": 0.5,
        }
    ]


@pytest.mark.parametrize(
    ("day", "expected_count"),
    [
        (date(2026, 3, 29), 46),
        (date(2026, 10, 25), 50),
    ],
)
def test_complete_dst_days_keep_elapsed_native_intervals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    day: date,
    expected_count: int,
) -> None:
    system = system_for_import(monkeypatch)
    SiteBinding.objects.filter(pk=system.binding.pk).update(timezone="Europe/Berlin")
    Region.objects.filter(pk=system.region.pk).update(timezone="Europe/Berlin")
    start, end = local_day_bounds(day, "Europe/Berlin")
    rows = [
        {
            "period_end": (start + timedelta(minutes=30 * (index + 1))).isoformat(),
            "period": "PT30M",
            "pv_power_rooftop": 0.5,
        }
        for index in range(expected_count)
    ]
    record = archive(system, power_file(tmp_path, rows))
    snapshot = ForecastSnapshot.objects.get(job__reference_import=record)
    assert snapshot.job.target_date == day
    assert snapshot.coverage_start == start
    assert snapshot.coverage_end == end
    assert len(snapshot.normalized_intervals) == expected_count
    assert history_day_complete(snapshot, day, "Europe/Berlin")
    energy = sum(float(row["power_kw"]) * 0.5 for row in snapshot.normalized_intervals)
    assert energy == 0.25 * expected_count


def test_archive_failure_rolls_back_all_days_before_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)

    def fail(*args: Any, **kwargs: Any) -> list[ForecastSnapshot]:
        raise RuntimeError("Injected database publication failure")

    monkeypatch.setattr(ForecastSnapshot.objects, "bulk_create", fail)
    with pytest.raises(RuntimeError, match="publication failure"):
        archive(system, power_file(tmp_path))
    assert not HistoricalReferenceImport.objects.exists()
    assert not ForecastSnapshot.objects.exists()
    assert not CollectionJob.objects.filter(endpoint="historical_import").exists()
    assert not AuditEvent.objects.filter(action="historical_reference_imported").exists()


def test_calibration_is_prepared_once_after_whole_archive_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    system.binding.refresh_from_db()
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        PlantParameters(name="Plant", dc_capacity_mw=1),
    )
    path = power_file(tmp_path)
    observed: list[int] = []

    def prepare(plant_id: int, now: datetime | None = None) -> None:
        with psycopg.connect(**connection.get_connection_params(), autocommit=True) as other:
            assert other.execute("SELECT count(*) FROM forecasts_forecastsnapshot").fetchone() == (
                2,
            )
        observed.append(plant_id)

    monkeypatch.setattr("pyracast.plants.preparation.prepare_calibration", prepare)
    record = archive(system, path)
    assert observed == [plant.pk]
    assert archive(system, path) == record
    assert observed == [plant.pk]


def test_database_uniqueness_preserves_regular_slots_and_import_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    record = archive(system, power_file(tmp_path))
    job = CollectionJob.objects.filter(reference_import=record).first()
    assert job is not None
    with pytest.raises(IntegrityError), transaction.atomic():
        CollectionJob.objects.create(
            binding=system.binding,
            reference_import=record,
            endpoint="historical_import",
            scheduled_for=NOW + timedelta(seconds=1),
            target_date=job.target_date,
            available_at=NOW,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        HistoricalReferenceImport.objects.create(
            binding=system.binding,
            content_sha256=record.content_sha256,
            source_name="duplicate",
            source_settings=record.source_settings,
            imported_by=system.admin,
            created_at=NOW,
            interval_count=3,
        )
    normal = CollectionJob.objects.filter(reference_import__isnull=True).first()
    assert normal is not None
    with pytest.raises(IntegrityError), transaction.atomic():
        CollectionJob.objects.create(
            binding=system.binding,
            endpoint=normal.endpoint,
            scheduled_for=normal.scheduled_for,
            target_date=normal.target_date,
            available_at=NOW,
        )


def test_concurrent_imports_publish_one_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    path = power_file(tmp_path)
    barrier = Barrier(2)

    def run() -> int:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return archive(system, path).pk
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))
    assert results[0] == results[1]
    assert HistoricalReferenceImport.objects.count() == 1
    assert ForecastSnapshot.objects.count() == 2


def test_command_imports_through_the_upload_operation_and_preserves_idempotence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    path = power_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    args = {
        "username": system.admin.username,
        "tenant_id": system.tenant.pk,
        "region_id": system.region.pk,
        "file": path,
        "reference": REFERENCE,
        "confirm": True,
    }
    output = StringIO()
    call_command("import_historical_reference", **args, stdout=output)
    assert "3 intervals" in output.getvalue()
    assert output.getvalue().strip().endswith("; imported.")
    repeated = StringIO()
    call_command("import_historical_reference", **args, stdout=repeated)
    assert "already imported" in repeated.getvalue()
    assert HistoricalReferenceImport.objects.count() == 1
    with pytest.raises(CommandError):
        call_command("import_historical_reference", **(args | {"username": "unknown"}))
    with pytest.raises(CommandError):
        call_command("import_historical_reference", **(args | {"username": system.member.username}))
    with pytest.raises(CommandError, match="working directory"):
        call_command(
            "import_historical_reference", **(args | {"file": tmp_path.parent / "outside.json"})
        )
    Region.objects.filter(pk=system.region.pk).update(status="pending", approved_binding=None)
    with pytest.raises(CommandError, match="approved reference connection"):
        call_command("import_historical_reference", **args)


def test_a_busy_plant_asks_the_command_to_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = system_for_import(monkeypatch)
    path = power_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    busy = OperationalError("lock timeout")
    busy.__cause__ = LockNotAvailable()
    failures = iter([busy, OperationalError("connection lost")])

    def locked(*args: object, **kwargs: object) -> None:
        raise next(failures)

    monkeypatch.setattr(command, "import_historical_upload", locked)
    options = {
        "username": system.admin.username,
        "tenant_id": system.tenant.pk,
        "region_id": system.region.pk,
        "file": path,
        "reference": REFERENCE,
        "confirm": True,
    }
    with pytest.raises(CommandError, match="busy"):
        call_command("import_historical_reference", **options)
    with pytest.raises(OperationalError):
        call_command("import_historical_reference", **options)


def test_the_export_loader_reads_only_inside_its_trusted_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = power_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    data, digest = load_json_file(path)
    assert digest == sha256(path.read_bytes()).hexdigest()
    assert isinstance(data, dict)
    assert len(data["estimated_actuals"]) == 3
    outside = tmp_path / "safe"
    outside.mkdir()
    with pytest.raises(ValueError, match="trusted directory"):
        load_json_file(path, trusted_root=outside)
