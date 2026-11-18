import io
import sys
from concurrent.futures import ThreadPoolExecutor
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
from django.db import close_old_connections, connection

from pyracast.accounts.models import Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.actuals import select_archived_actual_source
from pyracast.forecasts.coverage import intervals_cover_day
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.forecasts.snapshot_restore import RestoreSummary, restore_provider_snapshots
from pyracast.plants.models import ForecastIssuance
from pyracast.plants.operations import create_plant
from pyracast.regions.models import Region
from tests.support.domain import NOW, System
from tests.support.invariants import archived_rows, rewritten_archive_rows
from tests.support.provider_snapshot_restore import (
    SETTINGS,
    SITE,
    ZONE,
    BeforeReset,
    acquisition,
    binding_before_reset,
    collections_after_reset,
    collections_before_reset,
    encode_rows,
    export_content,
    export_rows,
    local,
    restore_system,
)

pytestmark = pytest.mark.django_db

KEY = "test-only-solcast-key"


def restore(system: System, content: bytes, *, dry_run: bool = False) -> RestoreSummary:
    return restore_provider_snapshots(
        system.admin,
        system.tenant.pk,
        system.region.pk,
        system.binding.pk,
        content,
        dry_run=dry_run,
    )


def outcomes(summary: RestoreSummary) -> dict[str, dict[str, int]]:
    return {item.endpoint: dict(item.outcomes) for item in summary.endpoints}


def restored_copy(binding: SiteBinding, original: ForecastSnapshot) -> ForecastSnapshot:
    return ForecastSnapshot.objects.select_related("job").get(
        binding=binding, raw_payload__snapshot_restore__snapshot_id=original.pk
    )


def restored_count(system: System) -> int:
    return ForecastSnapshot.objects.filter(
        binding=system.binding, raw_payload__has_key="snapshot_restore"
    ).count()


def live_ids(before: BeforeReset) -> set[int]:
    return {
        before.lookback.pk,
        *(night.pk for night in before.nights),
        *(forecast.pk for forecast in before.forecasts),
    }


def test_restored_export_serves_native_actuals_beside_the_historical_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    live = collections_after_reset(system, tmp_path)
    binding = system.binding
    gap = select_archived_actual_source(binding, date(2026, 10, 31), NOW)
    assert gap is not None
    assert gap.snapshot.job.endpoint == "historical_import"
    assert not intervals_cover_day(gap.intervals, date(2026, 10, 31), ZONE)
    assert select_archived_actual_source(binding, date(2026, 11, 1), NOW) is None
    stored = archived_rows()

    summary = restore(system, before.content)

    assert rewritten_archive_rows(stored, archived_rows()) == {}
    lookback = restored_copy(binding, before.lookback)
    imported = select_archived_actual_source(binding, date(2026, 10, 28), NOW)
    assert imported is not None
    assert imported.snapshot.job.endpoint == "historical_import"
    for offset in range(9):
        day = date(2026, 10, 30) + timedelta(days=offset)
        source = select_archived_actual_source(binding, day, NOW)
        assert source is not None
        assert {row.job.endpoint for row in source.snapshots} == {"estimated_actuals"}
        assert {row.job.reference_import_id for row in source.snapshots} == {None}
        assert intervals_cover_day(source.intervals, day, ZONE)
    for day in (date(2026, 10, 31), date(2026, 11, 1)):
        selected = select_archived_actual_source(binding, day, NOW)
        assert selected is not None
        assert selected.snapshot == lookback
    composed = select_archived_actual_source(binding, date(2026, 11, 2), NOW)
    assert composed is not None
    assert composed.composition is not None
    assert set(composed.composition.snapshot_ids) == {lookback.pk, live.pk}
    assert summary.days == (
        (date(2026, 10, 29), "historical import complete"),
        *((date(2026, 10, 30) + timedelta(days=offset), "native complete") for offset in range(9)),
    )


def test_restored_rows_keep_the_acquisition_and_take_the_live_job_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    attempts = RequestAttempt.objects.count()

    restore(system, before.content)

    original = before.lookback
    copy = restored_copy(system.binding, original)
    assert copy.raw_payload == {
        **original.raw_payload,
        "snapshot_restore": {
            "export_sha256": sha256(before.content).hexdigest(),
            "snapshot_id": original.pk,
            "restored_by_id": system.admin.pk,
            "restored_at": NOW.isoformat(),
        },
    }
    assert copy.normalized_intervals == original.normalized_intervals
    assert copy.normalized_intervals[-1]["power_kw"] is None
    for name in (
        "collected_at",
        "issued_at",
        "source_kind",
        "coverage_start",
        "coverage_end",
        "weather_payload",
    ):
        assert getattr(copy, name) == getattr(original, name)
    assert copy.available_at == original.collected_at
    assert copy.weather_error == ""
    job = copy.job
    assert (
        job.endpoint,
        job.status,
        job.scheduled_slot,
        job.target_date,
        job.retry_until,
        job.available_at,
        job.attempts,
        job.last_error,
        job.reference_import_id,
        job.lease_token,
        job.lease_expires_at,
    ) == (
        "estimated_actuals",
        "succeeded",
        original.collected_at,
        date(2026, 11, 5),
        original.collected_at + timedelta(minutes=15),
        original.collected_at,
        1,
        "",
        None,
        None,
        None,
    )
    night = restored_copy(system.binding, before.nights[0]).job
    assert (night.scheduled_slot, night.target_date, night.retry_until) == (
        datetime(2026, 11, 5, 21, 30, tzinfo=UTC),
        date(2026, 11, 5),
        local(date(2026, 11, 6), 9),
    )
    forecast = restored_copy(system.binding, before.forecasts[0])
    assert forecast.weather_payload == {"source": "open-meteo"}
    assert (forecast.job.endpoint, forecast.job.target_date, forecast.job.retry_until) == (
        "forecasts",
        date(2026, 11, 6),
        None,
    )
    result = ForecastResult.objects.get(snapshot=forecast)
    assert result.published_at == forecast.collected_at == forecast.available_at
    assert result.payload["local_date"] == "2026-11-06"
    assert result.payload["vintage"] == {
        "provider": "solcast",
        "source_kind": "forecast",
        "collected_at": forecast.collected_at.isoformat(),
        "issued_at": None,
        "binding_revision": f"{system.binding.pk}:{system.binding.region_revision}",
    }
    assert set(result.daily_payloads) == {"2026-11-05", "2026-11-06", "2026-11-07"}
    assert ForecastResult.objects.filter(snapshot__source_kind="forecast").count() == 2
    assert not ForecastResult.objects.filter(snapshot__source_kind="estimated_actuals").exists()
    assert RequestAttempt.objects.count() == attempts


def test_a_second_run_with_the_same_export_adds_nothing_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    first = restore(system, before.content)
    jobs = CollectionJob.objects.count()
    stored = archived_rows()

    second = restore(system, before.content)

    assert first.restored == 6
    assert second.restored == 0
    assert outcomes(second)["estimated_actuals"] == {"already_restored": 4, "other_site": 1}
    assert outcomes(second)["forecasts"] == {"already_restored": 2}
    assert second.days == first.days
    assert CollectionJob.objects.count() == jobs
    assert rewritten_archive_rows(stored, archived_rows()) == {}
    assert restored_count(system) == 6


def test_dry_run_reports_the_restore_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    jobs = CollectionJob.objects.count()
    snapshots = ForecastSnapshot.objects.count()

    planned = restore(system, before.content, dry_run=True)

    assert planned.dry_run
    assert CollectionJob.objects.count() == jobs
    assert ForecastSnapshot.objects.count() == snapshots
    assert not ForecastResult.objects.exists()
    applied = restore(system, before.content)
    assert not applied.dry_run
    assert outcomes(planned) == outcomes(applied)
    assert planned.days == applied.days
    assert planned.restored == applied.restored == 6


def test_rows_that_are_no_live_collection_of_the_site_are_skipped_and_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    rows = export_rows(before.content)
    lookback = next(row for row in rows if row["id"] == before.lookback.pk)
    rows.append(lookback | {"id": 10**9, "provider": "fixture"})

    summary = restore(system, encode_rows(rows))

    assert summary.rows == 10
    assert outcomes(summary) == {
        "estimated_actuals": {"restored": 4, "other_provider": 1, "other_site": 1},
        "forecasts": {"restored": 2},
        "historical_import": {"not_live": 1},
        "scaled_estimated_actuals": {"not_live": 1},
    }
    periods = {item.endpoint: item.period for item in summary.endpoints}
    assert periods == {
        "estimated_actuals": (
            datetime(2026, 10, 29, 7, tzinfo=UTC),
            datetime(2026, 11, 7, 21, tzinfo=UTC),
        ),
        "forecasts": (
            datetime(2026, 11, 4, 21, tzinfo=UTC),
            datetime(2026, 11, 8, 21, tzinfo=UTC),
        ),
        "historical_import": None,
        "scaled_estimated_actuals": None,
    }
    assert restored_count(system) == 6


def test_a_slot_that_holds_a_native_collection_is_skipped_and_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    slot = before.forecasts[0].collected_at
    held = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="forecasts",
        scheduled_slot=slot,
        target_date=date(2026, 11, 6),
        available_at=slot,
    )

    summary = restore(system, before.content)

    assert outcomes(summary)["forecasts"] == {"restored": 1, "slot_taken": 1}
    held.refresh_from_db()
    assert held.status == "pending"
    assert not ForecastSnapshot.objects.filter(job=held).exists()
    assert (
        CollectionJob.objects.filter(
            binding=system.binding, endpoint="forecasts", scheduled_slot=slot
        ).get()
        == held
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capacity_kw", 2),
        ("dc_capacity_kw", None),
        ("power_basis", "dc"),
        ("capacity_semantics_verified", False),
        ("capacity_semantics_verified", 1),
        ("azimuth_degrees", 180),
        ("losses_percent", 7),
        ("installation_date", "2025-01-01"),
        ("latitude", 41),
        ("longitude", 30),
        ("timezone", "Europe/Berlin"),
    ],
)
def test_a_differing_setting_refuses_the_whole_run_by_field_name(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    rows = export_rows(before.content)
    next(row for row in rows if row["id"] == before.nights[1].pk)["binding"][field] = value

    with pytest.raises(ValidationError) as refused:
        restore(system, encode_rows(rows))

    assert refused.value.messages == [
        f"The export's provider site differs from the binding in: {field}."
    ]
    assert restored_count(system) == 0


def test_only_the_settings_that_give_stored_numbers_their_meaning_are_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    rows = export_rows(before.content)
    site = next(row for row in rows if row["id"] == before.lookback.pk)["binding"]
    for name in SETTINGS:
        del site[name]
    site.update(region_id=0, provider_account_id=0, settings_verification_reference="other")

    with pytest.raises(ValidationError) as refused:
        restore(system, encode_rows(rows))

    assert refused.value.messages == [
        "The export's provider site differs from the binding in: "
        + ", ".join(sorted(SETTINGS))
        + "."
    ]
    assert SITE not in str(refused.value)


@pytest.mark.parametrize(
    ("model", "changes"),
    [
        ("account", {"provider": "fixture"}),
        ("account", {"credential_revision": 2}),
        ("binding", {"validated_at": None, "validated_credential_revision": None}),
        ("binding", {"capacity_semantics_verified": False}),
        ("binding", {"settings_verification_reference": ""}),
        ("binding", {"power_basis": "dc"}),
        ("region", {"status": "suspended"}),
        ("region", {"revision": 2}),
    ],
)
def test_the_target_binding_must_be_current_authorized_and_validated(
    monkeypatch: pytest.MonkeyPatch, model: str, changes: dict[str, Any]
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    instance = getattr(system, model)
    type(instance).objects.filter(pk=instance.pk).update(**changes)

    with pytest.raises(ValidationError, match="current authorized"):
        restore(system, before.content)

    assert restored_count(system) == 0


def test_platform_authority_and_tenant_region_binding_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    with pytest.raises(PermissionDenied):
        restore_provider_snapshots(
            system.member,
            system.tenant.pk,
            system.region.pk,
            system.binding.pk,
            before.content,
        )
    other = Tenant.objects.create(name="Other")
    with pytest.raises(Region.DoesNotExist):
        restore_provider_snapshots(
            system.admin, other.pk, system.region.pk, system.binding.pk, before.content
        )
    foreign = binding_before_reset(system)
    with pytest.raises(SiteBinding.DoesNotExist):
        restore_provider_snapshots(
            system.admin, system.tenant.pk, system.region.pk, foreign.pk, before.content
        )
    Region.objects.filter(pk=system.region.pk).update(deleted_at=NOW)
    with pytest.raises(Region.DoesNotExist):
        restore(system, before.content)
    assert restored_count(system) == 0


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"\x1f\x8b\x08truncated", "The export is not readable gzip."),
        (b"not json\n", "Export line 1 is not a provider snapshot."),
        (b'{"id": 1}\n\n[]\n', "Export line 1 is not a provider snapshot."),
        (b'{"id": NaN}\n', "Export line 1 is not a provider snapshot."),
        (b"\n[]\n", "Export line 2 is not a provider snapshot."),
    ],
)
def test_an_unreadable_export_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch, content: bytes, message: str
) -> None:
    system = restore_system(monkeypatch)

    with pytest.raises(ValidationError) as refused:
        restore(system, content)

    assert refused.value.messages == [message]


@pytest.mark.parametrize(
    "changes",
    [
        {"id": 0},
        {"id": True},
        {"provider": "other"},
        {"endpoint": "other"},
        {"source_kind": "other"},
        {"binding": []},
        {"binding": {"site_id": 7}},
    ],
)
def test_a_row_without_its_identity_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    rows = export_rows(before.content)
    rows[1].update(changes)

    with pytest.raises(ValidationError) as refused:
        restore(system, encode_rows(rows))

    assert refused.value.messages == ["Export line 2 is not a provider snapshot."]
    assert restored_count(system) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"source_kind": "forecast"},
        {"collected_at": "2026-11-09T13:00:01+00:00"},
        {"collected_at": "2026-11-05T07:00:00"},
        {"issued_at": 5},
        {"coverage_end": None},
        {"coverage_end": "2026-10-29T07:00:00+00:00"},
        {"raw_payload": []},
        {"raw_payload": {"snapshot_restore": {}}},
        {"normalized_intervals": {}},
        {"normalized_intervals": [["start"]]},
        {"normalized_intervals": [{"start": "2026-10-29T07:00:00+00:00", "power_kw": 1}]},
        {
            "normalized_intervals": [
                {"start": "2026-10-29T07:30:00+00:00", "end": "2026-10-29T07:00:00+00:00"}
            ]
        },
        {
            "normalized_intervals": [
                {
                    "start": "2026-10-29T07:00:00+00:00",
                    "end": "2026-10-29T07:30:00+00:00",
                    "power_kw": True,
                }
            ]
        },
        {
            "normalized_intervals": [
                {
                    "start": "2026-10-29T07:00:00+00:00",
                    "end": "2026-10-29T07:30:00+00:00",
                    "power_kw": "0.5",
                }
            ]
        },
        {"weather_payload": []},
    ],
)
def test_an_invalid_live_acquisition_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    rows = export_rows(before.content)
    line = next(number for number, row in enumerate(rows, 1) if row["id"] == before.lookback.pk)
    rows[line - 1].update(changes)

    with pytest.raises(ValidationError) as refused:
        restore(system, encode_rows(rows))

    assert refused.value.messages == [
        f"Export line {line} does not hold a restorable live acquisition."
    ]
    assert restored_count(system) == 0


def test_a_forecast_that_cannot_be_calculated_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    rows = export_rows(before.content)
    line = next(number for number, row in enumerate(rows, 1) if row["id"] == before.forecasts[1].pk)
    rows[line - 1]["normalized_intervals"][0]["sunlight"] = "eclipse"

    with pytest.raises(ValidationError) as refused:
        restore(system, encode_rows(rows))

    assert refused.value.messages == [f"Export line {line}: the forecast cannot be calculated."]
    assert restored_count(system) == 0
    assert not ForecastResult.objects.exists()


def test_the_day_report_names_partial_and_missing_native_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = binding_before_reset(system)
    acquisition(
        before,
        "estimated_actuals",
        local(date(2026, 11, 2), 1),
        local(date(2026, 11, 1)),
        local(date(2026, 11, 2)),
    )
    acquisition(
        before,
        "estimated_actuals",
        local(date(2026, 11, 4), 1),
        local(date(2026, 11, 3), 13),
        local(date(2026, 11, 4)),
    )

    summary = restore(system, export_content(compress=False))

    assert summary.days == (
        (date(2026, 11, 1), "native complete"),
        (date(2026, 11, 2), "missing"),
        (date(2026, 11, 3), "native partial"),
    )


def test_an_export_without_live_actuals_has_no_day_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = binding_before_reset(system)
    acquisition(
        before,
        "forecasts",
        datetime(2026, 11, 5, 3, tzinfo=UTC),
        datetime(2026, 11, 4, 21, tzinfo=UTC),
        datetime(2026, 11, 7, 21, tzinfo=UTC),
    )

    summary = restore(system, export_content())

    assert summary.days == ()
    assert outcomes(summary) == {"forecasts": {"restored": 1}}
    assert [item.period for item in summary.endpoints] == [
        (datetime(2026, 11, 4, 21, tzinfo=UTC), datetime(2026, 11, 7, 21, tzinfo=UTC))
    ]


def test_restored_actuals_request_calibration_preparation_after_commit(
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Any,
) -> None:
    system = restore_system(monkeypatch)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="Plant", dc_capacity_mw=1
    )
    before = collections_before_reset(system)
    observed: list[int] = []
    monkeypatch.setattr("pyracast.plants.preparation.prepare_calibration", observed.append)
    forecasts = encode_rows(
        [row for row in export_rows(before.content) if row["endpoint"] == "forecasts"]
    )

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        restore(system, before.content, dry_run=True)
        restore(system, forecasts)
    assert callbacks == []
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        restore(system, before.content)
    assert len(callbacks) == 1
    assert observed == [plant.pk]
    assert not ForecastIssuance.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_restores_store_each_snapshot_once(monkeypatch: pytest.MonkeyPatch) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    barrier = Barrier(2)

    def run() -> int:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return restore(system, before.content).restored
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(lambda _: run(), range(2)))

    assert results == [0, 6]
    assert restored_count(system) == 6


def command_arguments(system: System, export: str, *, username: str = "admin") -> list[str]:
    return [
        "--username",
        username,
        "--tenant-id",
        str(system.tenant.pk),
        "--region-id",
        str(system.region.pk),
        "--binding-id",
        str(system.binding.pk),
        "--export",
        export,
    ]


def test_command_restores_from_standard_input_and_prints_no_site_or_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    digest = sha256(before.content).hexdigest()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(before.content)))
    partial = {date(2026, 10, 29), date(2026, 11, 5)}
    days = [
        f"{day} native {'partial' if day in partial else 'complete'}"
        for day in (date(2026, 10, 29) + timedelta(days=offset) for offset in range(10))
    ]

    def report(verb: str, outcome: str) -> list[str]:
        return [
            f"Provider snapshot restore for binding {system.binding.pk}: 9 export rows, "
            f"SHA-256 {digest}.",
            f"estimated_actuals: 4 {verb}, 1 skipped, another provider site; covers "
            "2026-10-29T07:00:00+00:00 to 2026-11-07T21:00:00+00:00.",
            f"forecasts: 2 {verb}; covers 2026-11-04T21:00:00+00:00 to 2026-11-08T21:00:00+00:00.",
            "historical_import: 1 skipped, not a live collection (import its source file again).",
            "scaled_estimated_actuals: 1 skipped, not a live collection "
            "(normalize the reference capacity again).",
            "Native estimated actuals by local day (Europe/Istanbul):",
            *days,
            outcome,
        ]

    output = StringIO()
    call_command(
        "restore_provider_snapshots",
        *command_arguments(system, "-"),
        "--dry-run",
        stdout=output,
    )
    assert output.getvalue().splitlines() == report("to restore", "Dry run: nothing was written.")
    assert restored_count(system) == 0

    path = tmp_path / "export.solcast.jsonl.gz"
    path.write_bytes(before.content)
    output = StringIO()
    call_command("restore_provider_snapshots", *command_arguments(system, str(path)), stdout=output)
    assert output.getvalue().splitlines() == report("restored", "Restored 6 snapshots.")

    output = StringIO()
    call_command("restore_provider_snapshots", *command_arguments(system, str(path)), stdout=output)
    assert output.getvalue().splitlines()[1:3] == [
        "estimated_actuals: 4 already restored, 1 skipped, another provider site; covers "
        "2026-10-29T07:00:00+00:00 to 2026-11-07T21:00:00+00:00.",
        "forecasts: 2 already restored; covers "
        "2026-11-04T21:00:00+00:00 to 2026-11-08T21:00:00+00:00.",
    ]
    assert output.getvalue().splitlines()[-1] == "Nothing to restore."

    messages = [
        record.getMessage()
        for record in pyracast_logs.records
        if record.name == "pyracast.forecasts.snapshot_restore"
    ]
    assert messages == [
        f"provider_snapshots_restored binding_id={system.binding.pk} dry_run={dry_run} "
        f"restored={restored} skipped={skipped} export_sha256={digest}"
        for dry_run, restored, skipped in ((True, 6, 3), (False, 6, 3), (False, 0, 9))
    ]
    assert SITE not in pyracast_logs.text
    assert KEY not in pyracast_logs.text


def test_command_reports_refusals_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    path = tmp_path / "export.jsonl.gz"
    path.write_bytes(before.content)
    arguments = command_arguments(system, str(path))
    with pytest.raises(CommandError, match="Provider snapshot restore failed"):
        call_command("restore_provider_snapshots", *command_arguments(system, str(tmp_path / "x")))
    for username in ("unknown", system.member.username):
        with pytest.raises(CommandError, match="Provider snapshot restore failed"):
            call_command(
                "restore_provider_snapshots",
                *command_arguments(system, str(path), username=username),
            )
    rows = export_rows(before.content)
    rows[0]["binding"]["capacity_kw"] = 5
    path.write_bytes(encode_rows(rows))
    with pytest.raises(CommandError, match="differs from the binding in: capacity_kw") as refused:
        call_command("restore_provider_snapshots", *arguments)
    assert SITE not in str(refused.value)
    assert restored_count(system) == 0
    assert [
        record.getMessage()
        for record in pyracast_logs.records
        if record.name == "pyracast.forecasts.snapshot_restore"
    ] == [f"provider_snapshot_restore_refused binding_id={system.binding.pk}"]
