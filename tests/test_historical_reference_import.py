import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from io import StringIO
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.coverage import history_day_complete
from pyracast.forecasts.historical_import import (
    HistoricalSourceSettings,
    import_historical_reference,
    parse_source_settings,
)
from pyracast.forecasts.history import select_actual_snapshot
from pyracast.forecasts.models import CollectionJob, ForecastSnapshot, HistoricalReferenceImport
from pyracast.irradiance import local_day_bounds
from pyracast.irradiance.pairs import load_power
from pyracast.plants.models import Plant
from pyracast.plants.operations import create_plant
from pyracast.regions.models import AuditEvent, Region
from tests.test_domain import NOW, System, make_system
from tests.test_plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def source_settings(**updates: Any) -> HistoricalSourceSettings:
    return (
        HistoricalSourceSettings(
            product="historical_rooftop_pv",
            unit="kW",
            timestamp_convention="period_end",
            period="PT30M",
            latitude=40,
            longitude=29,
            capacity_kw=1,
            dc_capacity_kw=1,
            tilt_degrees=0,
            azimuth_degrees=0,
            losses_percent=0,
            mounting_type="fixed",
            source_reference="Authorized test historical export; declared model settings verified",
        )
        if not updates
        else replace(source_settings(), **updates)
    )


def system_for_import(monkeypatch: pytest.MonkeyPatch) -> System:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(provider="solcast")
    SiteBinding.objects.filter(pk=system.binding.pk).update(
        dc_capacity_kw=1, settings_verification_reference="Verified test model settings"
    )
    return system


def power_file(tmp_path: Path, rows: object | None = None) -> Path:
    if rows is None:
        rows = [
            {
                "period_end": "2026-11-07T22:30:00+02:00",
                "period": "PT30M",
                "pv_power_rooftop": 0,
            },
            {
                "period_end": "2026-11-07T23:00:00+02:00",
                "period": "PT30M",
                "pv_power_rooftop": 0.5,
            },
            {
                "period_end": "2026-11-08T00:00:00+02:00",
                "period": "PT30M",
                "pv_power_rooftop": 0,
            },
        ]
    path = tmp_path / "arbitrary-name.json"
    path.write_text(json.dumps({"estimated_actuals": rows}), encoding="utf-8")
    return path


def archive(system: System, path: Path, **updates: Any) -> HistoricalReferenceImport:
    return import_historical_reference(
        system.admin,
        system.tenant.pk,
        system.region.pk,
        system.binding.pk,
        path,
        source_settings(**updates),
        trusted_root=path.parent,
    )


def test_archive_preserves_instants_missingness_and_actual_import_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    path = power_file(tmp_path)
    attempts = system.account.requestattempt_set.count()
    record = archive(system, path)
    assert record.imported_at == NOW
    assert record.imported_by == system.admin
    assert record.source_name == path.name
    assert record.sha256 == sha256(path.read_bytes()).hexdigest()
    assert record.interval_count == 3
    assert record.source_settings == asdict(source_settings())
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
        assert snapshot.collected_at == snapshot.available_at == snapshot.job.scheduled_slot == NOW
        assert snapshot.issued_at is None
        assert snapshot.job.created_at == NOW
        assert snapshot.job.available_at == NOW
        assert snapshot.job.status == "succeeded"
        assert snapshot.job.endpoint == "historical_import"
        assert snapshot.job.attempts == 0
        assert snapshot.raw_payload["historical_import"] == {
            "id": record.pk,
            "sha256": record.sha256,
            "source_settings": record.source_settings,
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
    with pytest.raises(ValidationError, match="different source declaration"):
        archive(system, path, source_reference="Changed declaration")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["estimated_actuals"][0]["pv_power_rooftop"] = 0.2
    path.write_text(json.dumps(payload), encoding="utf-8")
    corrected = archive(system, path)
    assert corrected != original
    assert corrected.sha256 != original.sha256
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
        native.save()
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
    second.save()
    assert select_actual_snapshot(system.binding, day, NOW) == first


@pytest.mark.parametrize("value", [None, {}, {"unit": "kW"}])
def test_source_declaration_requires_complete_exact_schema(value: object) -> None:
    with pytest.raises(ValueError, match="complete historical source declaration"):
        parse_source_settings(value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("product", "forecasts"),
        ("unit", "MW"),
        ("period", "PT60M"),
        ("timestamp_convention", "period_start"),
        ("mounting_type", "tracking"),
        ("source_reference", ""),
        ("source_reference", "  "),
        ("source_reference", 1),
        ("source_reference", "x" * 1001),
        ("capacity_kw", True),
        ("capacity_kw", "1000"),
        ("capacity_kw", 10**400),
        ("capacity_kw", float("inf")),
        ("latitude", -91),
        ("longitude", 181),
        ("capacity_kw", 0),
        ("dc_capacity_kw", 0),
        ("tilt_degrees", 10),
        ("azimuth_degrees", -181),
        ("losses_percent", 100),
    ],
)
def test_source_declaration_rejects_wrong_units_geometry_and_nonfinite_values(
    key: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=r"Unsupported|reference|required|finite|Invalid"):
        parse_source_settings(asdict(source_settings()) | {key: value})


def test_source_declaration_rejects_unknown_fields_and_trims_reference() -> None:
    with pytest.raises(ValueError, match="complete historical source declaration"):
        parse_source_settings(asdict(source_settings()) | {"api_key": "not-a-real-secret"})
    parsed = parse_source_settings(asdict(source_settings(source_reference="  approval  ")))
    assert parsed.source_reference == "approval"


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
    with pytest.raises(ValidationError, match="do not match"):
        archive(system, power_file(tmp_path), **{key: value})
    assert not HistoricalReferenceImport.objects.exists()
    assert not ForecastSnapshot.objects.exists()


@pytest.mark.parametrize(
    ("model", "changes"),
    [
        ("account", {"enabled": False}),
        ("account", {"authorized_at": None}),
        ("account", {"authorized_by_id": None}),
        ("account", {"authorization_reference": ""}),
        ("account", {"provider": "fixture"}),
        ("account", {"credential_revision": 2}),
        ("binding", {"validated_at": None}),
        ("binding", {"capacity_semantics_verified": False}),
        ("binding", {"capacity_basis": "dc"}),
        ("binding", {"power_basis": "dc"}),
        ("binding", {"settings_verification_reference": ""}),
        ("region", {"status": "suspended"}),
        ("region", {"revision": 2}),
    ],
)
def test_import_requires_current_authorized_validated_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    changes: dict[str, Any],
) -> None:
    system = system_for_import(monkeypatch)
    instance = getattr(system, model)
    type(instance).objects.filter(pk=instance.pk).update(**changes)
    with pytest.raises(ValidationError, match="current authorized"):
        archive(system, power_file(tmp_path))
    assert not HistoricalReferenceImport.objects.exists()


def test_admin_and_tenant_region_binding_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    path = power_file(tmp_path)
    args = (system.tenant.pk, system.region.pk, system.binding.pk, path, source_settings())
    with pytest.raises(PermissionDenied):
        import_historical_reference(system.member, *args, trusted_root=tmp_path)
    other = Tenant.objects.create(name="Other")
    with pytest.raises(Region.DoesNotExist):
        import_historical_reference(system.admin, other.pk, *args[1:], trusted_root=tmp_path)
    Region.objects.filter(pk=system.region.pk).update(deleted_at=NOW)
    with pytest.raises(Region.DoesNotExist):
        import_historical_reference(system.admin, *args, trusted_root=tmp_path)
    Region.objects.filter(pk=system.region.pk).update(deleted_at=None)
    with pytest.raises(SiteBinding.DoesNotExist):
        import_historical_reference(
            system.admin,
            system.tenant.pk,
            system.region.pk,
            system.binding.pk + 100,
            path,
            source_settings(),
            trusted_root=tmp_path,
        )
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
    with pytest.raises(ValidationError, match=r"completed|future"):
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
    creator = ForecastSnapshot.objects.create

    def fail_second(**kwargs: Any) -> ForecastSnapshot:
        if ForecastSnapshot.objects.exists():
            raise RuntimeError("Injected database publication failure")
        return creator(**kwargs)

    monkeypatch.setattr(ForecastSnapshot.objects, "create", fail_second)
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
        system.member, system.tenant.pk, system.region.pk, name="Plant", dc_capacity_mw=1
    )
    Plant.objects.create(region=system.region, name="Unconfigured", deleted_at=NOW)
    path = power_file(tmp_path)
    observed: list[int] = []

    def prepare(plant_id: int) -> None:
        assert not connection.in_atomic_block
        assert ForecastSnapshot.objects.count() == 2
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
            scheduled_slot=NOW + timedelta(seconds=1),
            target_date=job.target_date,
            available_at=NOW,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        HistoricalReferenceImport.objects.create(
            binding=system.binding,
            sha256=record.sha256,
            source_name="duplicate",
            source_settings=record.source_settings,
            imported_by=system.admin,
            imported_at=NOW,
            interval_count=3,
        )
    normal = CollectionJob.objects.filter(reference_import__isnull=True).first()
    assert normal is not None
    with pytest.raises(IntegrityError), transaction.atomic():
        CollectionJob.objects.create(
            binding=system.binding,
            endpoint=normal.endpoint,
            scheduled_slot=normal.scheduled_slot,
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


def test_command_reads_trusted_manifest_and_preserves_idempotence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    path = power_file(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(asdict(source_settings())), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    args = {
        "username": system.admin.username,
        "tenant_id": system.tenant.pk,
        "region_id": system.region.pk,
        "binding_id": system.binding.pk,
        "power_file": path,
        "manifest": manifest,
    }
    output = StringIO()
    call_command("import_historical_reference", **args, stdout=output)
    assert "3 native intervals" in output.getvalue()
    call_command("import_historical_reference", **args, stdout=StringIO())
    assert HistoricalReferenceImport.objects.count() == 1
    with pytest.raises(CommandError):
        call_command("import_historical_reference", **(args | {"username": "unknown"}))
    with pytest.raises(CommandError):
        call_command("import_historical_reference", **(args | {"username": system.member.username}))
    with pytest.raises(CommandError, match="trusted directory"):
        call_command(
            "import_historical_reference", **(args | {"manifest": tmp_path.parent / "outside.json"})
        )
    manifest.write_text(json.dumps(asdict(source_settings()) | {"unit": "MW"}), encoding="utf-8")
    with pytest.raises(CommandError, match="Unsupported historical"):
        call_command("import_historical_reference", **args)


def test_power_file_root_guard_and_whole_interval_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = power_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    loaded = load_power(path)
    assert loaded.sha256 == sha256(path.read_bytes()).hexdigest()
    assert len(loaded.intervals) == len(loaded.raw_rows) == 3
    assert loaded.intervals[0].start == datetime(2026, 11, 7, 20, tzinfo=UTC)
    assert loaded.intervals[0].power_kw == 0
    outside = tmp_path / "safe"
    outside.mkdir()
    with pytest.raises(ValueError, match="trusted directory"):
        load_power(path, trusted_root=outside)
