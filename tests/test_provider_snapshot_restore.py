import gzip
import io
import logging
import re
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
from django.core.management import CommandError, call_command
from django.db import close_old_connections, connection
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.calculation import calculate
from pyracast.collection_window import INTRADAY_HISTORY_WINDOW
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.actuals import select_archived_actual_source
from pyracast.forecasts.coverage import intervals_cover_day
from pyracast.forecasts.intraday import native_actual_snapshots
from pyracast.forecasts.jobs.claim import claim
from pyracast.forecasts.management.commands.restore_provider_snapshots import Command
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.forecasts.projections import prepare_daily_payloads
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.forecasts.snapshot_restore import RestoreSummary, restore_provider_snapshots
from pyracast.forecasts.sources import snapshot_data
from pyracast.notifications import scheduler as notification_scheduler
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.preparation import prepare_delivery
from pyracast.plants.models import ForecastIssuance
from pyracast.plants.operations import create_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.models import Region
from tests.support.domain import NOW, System
from tests.support.invariants import archived_rows, rewritten_archive_rows
from tests.support.provider_snapshot_restore import (
    OTHER_SITE,
    SEPARATOR,
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
    job_fields,
    local,
    replacement_binding,
    restore_system,
)

pytestmark = pytest.mark.django_db(transaction=True)

KEY = "test-only-solcast-key"
COMMAND_LOGGER = "pyracast.forecasts.management.commands.restore_provider_snapshots"
DROP = object()


def restore(system: System, content: bytes, **options: Any) -> RestoreSummary:
    return restore_provider_snapshots(
        system.admin,
        system.tenant.pk,
        system.region.pk,
        system.binding.pk,
        content,
        **options,
    )


def outcomes(summary: RestoreSummary) -> dict[str, dict[str, int]]:
    return {item.endpoint: dict(item.outcomes) for item in summary.endpoints}


def collection_clocks(system: System, *times: str) -> None:
    """Clocks configured a week before the binding's approval at NOW (16:00 local)."""
    CollectionSchedule.objects.create(
        region=system.region,
        enabled=True,
        times=list(times),
        effective_from=NOW - timedelta(days=7),
    )


def restored_copy(binding: SiteBinding, original: ForecastSnapshot) -> ForecastSnapshot:
    return ForecastSnapshot.objects.select_related("job").get(
        binding=binding, raw_payload__snapshot_restore__snapshot_id=original.pk
    )


def restored_count(system: System) -> int:
    return ForecastSnapshot.objects.filter(
        binding=system.binding, raw_payload__has_key="snapshot_restore"
    ).count()


EXPORTED_DATE = date(2025, 3, 1)
RECORDED_DATE = date(2025, 3, 4)
ACCEPTED_LINE = (
    'Accepted recorded difference in installation_date: export "2025-03-01"; binding "2025-03-04".'
)


def installation_date_recorded_differently(system: System) -> BeforeReset:
    SiteBinding.objects.filter(pk=system.binding.pk).update(installation_date=EXPORTED_DATE)
    system.binding.refresh_from_db()
    before = collections_before_reset(system)
    SiteBinding.objects.filter(pk=system.binding.pk).update(installation_date=RECORDED_DATE)
    system.binding.refresh_from_db()
    return before


def test_restored_export_serves_native_actuals_beside_the_historical_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    approval = CollectionJob.objects.get(binding=system.binding)
    live = collections_after_reset(system, tmp_path)
    assert approval.scheduled_for <= live.job.scheduled_for < live.collected_at
    later = NOW + timedelta(hours=1)
    monkeypatch.setattr(timezone, "now", lambda: later)
    binding = system.binding
    gap = select_archived_actual_source(binding, date(2026, 10, 31), later)
    assert gap is not None
    assert gap.snapshot.job.endpoint == "historical_import"
    assert not intervals_cover_day(gap.intervals, date(2026, 10, 31), ZONE)
    assert select_archived_actual_source(binding, date(2026, 11, 1), later) is None
    stored = archived_rows()

    summary = restore(system, before.content)

    assert rewritten_archive_rows(stored, archived_rows()) == {}
    lookback = restored_copy(binding, before.lookback)
    imported = select_archived_actual_source(binding, date(2026, 10, 28), later)
    assert imported is not None
    assert imported.snapshot.job.endpoint == "historical_import"
    for offset in range(9):
        day = date(2026, 10, 30) + timedelta(days=offset)
        source = select_archived_actual_source(binding, day, later)
        assert source is not None
        assert {row.job.endpoint for row in source.snapshots} == {"estimated_actuals"}
        assert {row.job.reference_import_id for row in source.snapshots} == {None}
        assert intervals_cover_day(source.intervals, day, ZONE)
    for day in (date(2026, 10, 31), date(2026, 11, 1)):
        selected = select_archived_actual_source(binding, day, later)
        assert selected is not None
        assert selected.snapshot == lookback
    composed = select_archived_actual_source(binding, date(2026, 11, 2), later)
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
    assert SEPARATOR.encode() in gzip.decompress(before.content)
    assert copy.raw_payload["note"] == "line" + SEPARATOR + "separator"
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
        "available_at",
        "weather_payload",
    ):
        assert getattr(copy, name) == getattr(original, name)
    assert copy.weather_error == ""
    forecast = restored_copy(system.binding, before.forecasts[0])
    assert forecast.weather_payload == {"source": "open-meteo"}
    assert forecast.job.target_date == date(2026, 11, 6)
    result = ForecastResult.objects.get(snapshot=forecast)
    assert forecast.available_at == before.forecasts[0].available_at
    assert result.published_at == forecast.available_at != forecast.collected_at
    assert result.payload["local_date"] == "2026-11-06"
    assert result.payload["vintage"] == {
        "provider": "solcast",
        "source_kind": "forecast",
        "collected_at": forecast.collected_at.isoformat(),
        "issued_at": None,
        "binding_revision": f"{system.binding.pk}:{system.binding.region_revision}",
    }
    assert set(result.daily_payloads) == {"2026-11-05", "2026-11-06", "2026-11-07"}
    assert ForecastResult.objects.filter(snapshot__source_kind="forecast").count() == 3
    assert not ForecastResult.objects.filter(snapshot__source_kind="estimated_actuals").exists()
    assert RequestAttempt.objects.count() == attempts


def test_restored_jobs_hold_the_exported_job_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    exported = {row["id"]: row["job"] for row in export_rows(before.content)}

    restore(system, before.content)

    assert {name for job in exported.values() for name in job} == {
        field.attname for field in CollectionJob._meta.concrete_fields
    }
    for original in before.live:
        copy = restored_copy(system.binding, original).job
        assert exported[original.pk]["id"] == original.job_id
        assert job_fields(copy) == job_fields(original.job)
        assert copy.binding_id == system.binding.pk
        assert copy.created_at == NOW


def test_restored_actuals_are_admitted_at_their_slot_as_the_exported_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    source = before.lookback.binding

    restore(system, before.content)

    admitted = []
    for original in before.actuals:
        copy = restored_copy(system.binding, original)
        slot, start, end = (
            original.job.scheduled_for,
            original.coverage_start,
            original.coverage_end,
        )
        assert start is not None
        assert end is not None
        for grace in (None, slot + INTRADAY_HISTORY_WINDOW):
            exported = native_actual_snapshots(source, start, end, slot, actuals_grace_until=grace)
            held = native_actual_snapshots(
                system.binding, start, end, slot, actuals_grace_until=grace
            )
            assert (original in exported) == (copy in held)
            if copy in held:
                admitted.append((original, grace is not None))
    assert admitted == [
        (before.lookback, True),
        (before.intraday, True),
        (before.nights[0], True),
        (before.nights[2], True),
    ]


def test_restored_actuals_published_after_the_grace_stay_outside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    erased = binding_before_reset(system)
    slot = local(date(2026, 11, 7), 10)
    original = acquisition(
        erased,
        "estimated_actuals",
        slot,
        local(date(2026, 11, 7)),
        slot,
        collected_at=slot + timedelta(minutes=14, seconds=59),
    )
    assert original.available_at == slot + timedelta(minutes=15, seconds=59)

    restore(system, export_content())

    copy = restored_copy(system.binding, original)
    graces = (slot + INTRADAY_HISTORY_WINDOW, slot + timedelta(minutes=16))
    exported = [
        original
        in native_actual_snapshots(
            erased, local(date(2026, 11, 7)), slot, slot, actuals_grace_until=grace
        )
        for grace in graces
    ]
    held = [
        copy
        in native_actual_snapshots(
            system.binding, local(date(2026, 11, 7)), slot, slot, actuals_grace_until=grace
        )
        for grace in graces
    ]
    assert exported == held == [False, True]
    assert copy.available_at == original.available_at


def test_the_approval_acquisition_still_runs_after_older_forecasts_are_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    approval = CollectionJob.objects.get(binding=system.binding)
    collection_clocks(system, "08:00", "10:00")
    restore(system, collections_before_reset(system).content)

    lease = claim(approval.pk, NOW)

    approval.refresh_from_db()
    assert lease is not None
    assert (approval.status, approval.last_error) == ("running", "")


def test_the_scheduler_backfills_no_slot_before_approval_after_a_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    collection_clocks(system, "08:00", "10:00")
    restore(system, collections_before_reset(system).content)
    jobs = CollectionJob.objects.count()

    assert enqueue_due(NOW + timedelta(minutes=30)) == []
    assert CollectionJob.objects.count() == jobs


def test_a_reminder_before_approval_does_not_wait_for_a_collection_after_a_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    collection_clocks(system, "15:50")
    restore(system, collections_before_reset(system).content)
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=["owner@example.com"],
        times=["15:50"],
        now=NOW - timedelta(days=7),
    )
    notification_scheduler.enqueue_due(NOW + timedelta(seconds=30))
    delivery = NotificationDelivery.objects.get(
        schedule__region=system.region, scheduled_for=local(date(2026, 11, 9), 15, 50)
    )
    assert delivery.preparation_deadline > NOW + timedelta(minutes=1)

    assert prepare_delivery(delivery, NOW + timedelta(minutes=1))
    assert delivery.prepared_message is not None


def test_a_collection_whose_retry_window_was_open_at_approval_is_left_to_the_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    night = date(2026, 11, 10)
    approved, window_end = local(night, 1), local(night, 9)
    erased = binding_before_reset(system)
    closed = acquisition(
        erased,
        "estimated_actuals",
        local(date(2026, 11, 9)),
        local(date(2026, 11, 2)),
        local(date(2026, 11, 9)),
        target_date=date(2026, 11, 8),
        retry_until=local(date(2026, 11, 9), 9),
    )
    incomplete = acquisition(
        erased,
        "estimated_actuals",
        local(night),
        local(date(2026, 11, 3)),
        local(date(2026, 11, 9), 12),
        target_date=date(2026, 11, 9),
        retry_until=window_end,
        available_at=local(night, 0, 30),
        last_error="history_incomplete",
    )
    follow_up = local(night, 0, 31, 4)
    CollectionJob.objects.create(
        binding=erased,
        endpoint="estimated_actuals",
        scheduled_for=follow_up,
        target_date=date(2026, 11, 9),
        available_at=follow_up,
        retry_until=window_end,
    )
    content = export_content()
    fresh = replacement_binding(system, approved_at=approved)
    monkeypatch.setattr(timezone, "now", lambda: approved + timedelta(minutes=2))

    summary = restore_provider_snapshots(
        system.admin, system.tenant.pk, system.region.pk, fresh.pk, content
    )
    sweep = approved + timedelta(minutes=5)
    leases = [claim(job, sweep) for job in enqueue_due(sweep)]

    assert [(lease.endpoint, lease.target_date) for lease in leases if lease is not None] == [
        ("estimated_actuals", date(2026, 11, 9))
    ]
    assert outcomes(summary) == {"estimated_actuals": {"restored": 1, "open_at_approval": 1}}
    assert restored_copy(fresh, closed).job.retry_until == local(date(2026, 11, 9), 9)
    assert not ForecastSnapshot.objects.filter(
        binding=fresh, raw_payload__snapshot_restore__snapshot_id=incomplete.pk
    ).exists()


def test_a_collection_not_before_the_approval_collection_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    rows = export_rows(before.content)
    line = next(number for number, row in enumerate(rows, 1) if row["id"] == before.lookback.pk)
    rows[line - 1].update(collected_at=NOW.isoformat())
    rows[line - 1]["job"].update(scheduled_for=NOW.isoformat())

    with pytest.raises(ValidationError) as refused:
        restore(system, encode_rows(rows))

    assert refused.value.messages == [
        f"Export line {line} is not before the binding's approval collection: job.scheduled_for."
    ]
    assert restored_count(system) == 0


def test_a_binding_without_its_approval_collection_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    CollectionJob.objects.filter(binding=system.binding).delete()

    with pytest.raises(ValidationError) as refused:
        restore(system, before.content)

    assert refused.value.messages == ["The binding holds no approval collection."]
    assert restored_count(system) == 0


def test_a_second_run_with_the_same_export_adds_nothing_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    first = restore(system, before.content)
    jobs = CollectionJob.objects.count()
    stored = archived_rows()

    second = restore(system, before.content)
    reencoded = restore(system, gzip.decompress(before.content))

    assert first.restored == 8
    assert second.restored == reencoded.restored == 0
    assert outcomes(second) == outcomes(reencoded)
    assert outcomes(second)["estimated_actuals"] == {"already_restored": 5}
    assert outcomes(second)["forecasts"] == {"already_restored": 3}
    assert second.days == first.days
    assert CollectionJob.objects.count() == jobs
    assert rewritten_archive_rows(stored, archived_rows()) == {}
    assert restored_count(system) == 8


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
    assert planned.restored == applied.restored == 8


def test_rows_that_are_no_live_collection_of_the_provider_are_skipped_and_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    rows = export_rows(before.content)
    lookback = next(row for row in rows if row["id"] == before.lookback.pk)
    rows.append(lookback | {"id": 10**9, "provider": "fixture"})

    summary = restore(system, encode_rows(rows))

    assert summary.rows == 11
    assert outcomes(summary) == {
        "estimated_actuals": {"restored": 5, "other_provider": 1},
        "forecasts": {"restored": 3},
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
    assert restored_count(system) == 8


def test_a_slot_that_holds_a_native_collection_is_skipped_and_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    slot = before.forecasts[0].job.scheduled_for
    held = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="forecasts",
        scheduled_for=slot,
        target_date=date(2026, 11, 6),
        available_at=slot,
    )

    summary = restore(system, before.content)

    assert outcomes(summary)["forecasts"] == {"restored": 2, "slot_taken": 1}
    held.refresh_from_db()
    assert held.status == "pending"
    assert not ForecastSnapshot.objects.filter(job=held).exists()
    assert not ForecastSnapshot.objects.filter(
        binding=system.binding, collected_at=before.forecasts[0].collected_at
    ).exists()
    assert (
        CollectionJob.objects.filter(
            binding=system.binding, endpoint="forecasts", scheduled_for=slot
        ).get()
        == held
    )


def test_an_acquisition_the_binding_already_holds_at_the_slot_is_skipped_and_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    original = before.forecasts[0]
    slot = original.job.scheduled_for
    held = acquisition(
        system.binding,
        "forecasts",
        slot,
        datetime(2026, 11, 4, 21, tzinfo=UTC),
        datetime(2026, 11, 7, 21, tzinfo=UTC),
        collected_at=slot + timedelta(minutes=7),
    )

    summary = restore(system, before.content)

    assert outcomes(summary)["forecasts"] == {"restored": 2, "slot_taken": 1}
    assert list(
        ForecastSnapshot.objects.filter(
            binding=system.binding, job__endpoint="forecasts", job__scheduled_for=slot
        ).values_list("pk", flat=True)
    ) == [held.pk]
    assert not ForecastSnapshot.objects.filter(
        binding=system.binding, collected_at=original.collected_at
    ).exists()


def test_a_reused_snapshot_id_of_another_export_is_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    restore(system, before.content)
    original = before.forecasts[0]
    reused = next(row for row in export_rows(before.content) if row["id"] == original.pk)
    reused["collected_at"] = (original.collected_at + timedelta(hours=12)).isoformat()
    reused["job"]["scheduled_for"] = (original.job.scheduled_for + timedelta(hours=12)).isoformat()

    summary = restore(system, encode_rows([reused]))

    assert outcomes(summary) == {"forecasts": {"restored": 1}}
    assert sorted(
        ForecastSnapshot.objects.filter(
            binding=system.binding, raw_payload__snapshot_restore__snapshot_id=original.pk
        ).values_list("collected_at", flat=True)
    ) == [original.collected_at, original.collected_at + timedelta(hours=12)]


def test_rows_restored_before_a_later_reset_are_restored_again_with_their_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    restore(system, before.content)
    later = encode_rows(
        [row for row in export_rows(export_content()) if row["binding"]["id"] == system.binding.pk]
    )
    fresh = replacement_binding(system)

    summary = restore_provider_snapshots(
        system.admin, system.tenant.pk, system.region.pk, fresh.pk, later
    )

    assert outcomes(summary) == {
        "estimated_actuals": {"restored": 5},
        "forecasts": {"restored": 3},
    }
    for original in before.live:
        first = restored_copy(system.binding, original)
        second = ForecastSnapshot.objects.select_related("job").get(
            binding=fresh, raw_payload__snapshot_restore__snapshot_id=first.pk
        )
        assert job_fields(second.job) == job_fields(original.job)
        assert second.raw_payload == {
            **original.raw_payload,
            "snapshot_restore": {
                "export_sha256": sha256(later).hexdigest(),
                "snapshot_id": first.pk,
                "restored_by_id": system.admin.pk,
                "restored_at": NOW.isoformat(),
                "previous": first.raw_payload["snapshot_restore"],
            },
        }
        assert second.normalized_intervals == original.normalized_intervals


def test_an_export_of_another_provider_site_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    collections_before_reset(system)
    acquisition(
        binding_before_reset(system, site_id=OTHER_SITE),
        "estimated_actuals",
        datetime(2026, 11, 6, 21, 30, tzinfo=UTC),
        datetime(2026, 11, 5, 21, tzinfo=UTC),
        datetime(2026, 11, 6, 21, tzinfo=UTC),
    )

    with pytest.raises(ValidationError) as refused:
        restore(system, export_content())

    assert refused.value.messages == [
        "The export's provider site differs from the binding in: site_id."
    ]
    assert restored_count(system) == 0


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


def test_an_accepted_recorded_setting_restores_and_names_both_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = installation_date_recorded_differently(system)

    summary = restore(system, before.content, accepted=("installation_date",))

    assert summary.restored == 8
    assert [(item.name, item.exported, item.binding) for item in summary.accepted] == [
        ("installation_date", ('"2025-03-01"',), '"2025-03-04"')
    ]


@pytest.mark.parametrize(("field", "value"), [("capacity_kw", 2), ("site_id", OTHER_SITE)])
def test_an_accepted_recorded_setting_never_hides_another_difference(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    system = restore_system(monkeypatch)
    before = installation_date_recorded_differently(system)
    rows = export_rows(before.content)
    next(row for row in rows if row["id"] == before.nights[1].pk)["binding"][field] = value

    with pytest.raises(ValidationError) as refused:
        restore(system, encode_rows(rows), accepted=("installation_date",))

    assert refused.value.messages == [
        f"The export's provider site differs from the binding in: {field}."
    ]
    assert restored_count(system) == 0


def test_accepting_a_setting_that_does_not_differ_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)

    with pytest.raises(ValidationError) as refused:
        restore(system, before.content, accepted=("installation_date",))

    assert refused.value.messages == [
        "The export's provider site does not differ from the binding in: installation_date."
    ]
    assert restored_count(system) == 0


@pytest.mark.parametrize("name", ["capacity_kw", "site_id", "timezone"])
def test_only_a_recorded_setting_can_be_accepted(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    system = restore_system(monkeypatch)
    before = installation_date_recorded_differently(system)
    rows = export_rows(before.content)
    for row in rows:
        row["binding"][name] = {"capacity_kw": 2, "site_id": OTHER_SITE}.get(name, "UTC")

    with pytest.raises(ValidationError) as refused:
        restore(system, encode_rows(rows), accepted=("installation_date", name))

    assert refused.value.messages == [f"Only a recorded setting can be accepted, not: {name}."]
    assert restored_count(system) == 0


def test_rows_restored_with_an_accepted_difference_record_both_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = installation_date_recorded_differently(system)
    forecasts = encode_rows(
        [row for row in export_rows(before.content) if row["endpoint"] == "forecasts"]
    )
    restore(system, forecasts, accepted=("installation_date",))
    later = NOW + timedelta(hours=1)
    monkeypatch.setattr(timezone, "now", lambda: later)

    summary = restore(system, before.content, accepted=("installation_date",))

    assert outcomes(summary) == {
        "estimated_actuals": {"restored": 5},
        "forecasts": {"already_restored": 3},
        "historical_import": {"not_live": 1},
        "scaled_estimated_actuals": {"not_live": 1},
    }
    accepted = {"installation_date": {"export": "2025-03-01", "binding": "2025-03-04"}}
    for content, moment, originals in (
        (forecasts, NOW, (before.approval, *before.forecasts)),
        (before.content, later, before.actuals),
    ):
        for original in originals:
            assert restored_copy(system.binding, original).raw_payload["snapshot_restore"] == {
                "export_sha256": sha256(content).hexdigest(),
                "snapshot_id": original.pk,
                "restored_by_id": system.admin.pk,
                "restored_at": moment.isoformat(),
                "accepted_differences": accepted,
            }


def test_an_accepted_difference_leaves_the_calculated_forecast_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = installation_date_recorded_differently(system)

    restore(system, before.content, accepted=("installation_date",))

    exported = SiteBinding.objects.get(pk=system.binding.pk)
    exported.installation_date = EXPORTED_DATE
    for original in (before.approval, *before.forecasts):
        snapshot = restored_copy(system.binding, original)
        result = ForecastResult.objects.get(snapshot=snapshot)
        data = snapshot_data(snapshot)
        day = snapshot.job.target_date
        expected = calculate(exported, day, data)
        assert result.payload == expected
        assert result.daily_payloads == prepare_daily_payloads(
            exported, day, data, calculate, expected
        )


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
        {"available_at": None},
        {"available_at": "2026-11-05T07:01:00"},
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


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("job", DROP),
        ("job", None),
        ("job", []),
        ("endpoint", "forecasts"),
        ("status", "pending"),
        ("reference_import_id", 1),
        ("scheduled_for", DROP),
        ("scheduled_for", "2026-11-05T07:00:05+00:00"),
        ("scheduled_for", "2026-11-05T07:00:00"),
        ("target_date", "2026-11-31"),
        ("target_date", 20261105),
        ("available_at", None),
        ("retry_until", "soon"),
        ("attempts", -1),
        ("attempts", True),
        ("attempts", 1.0),
        ("last_error", "unknown"),
        ("last_error", None),
        ("lease_token", "00000000-0000-0000-0000-000000000001"),
        ("lease_expires_at", "2026-11-05T07:05:00+00:00"),
    ],
)
def test_a_row_without_a_succeeded_live_job_refuses_the_run_by_field_name(
    monkeypatch: pytest.MonkeyPatch, key: str, value: object
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    rows = export_rows(before.content)
    line = next(number for number, row in enumerate(rows, 1) if row["id"] == before.lookback.pk)
    holder, field = (
        (rows[line - 1], "job") if key == "job" else (rows[line - 1]["job"], f"job.{key}")
    )
    if value is DROP:
        del holder[key]
    else:
        holder[key] = value

    with pytest.raises(ValidationError) as refused:
        restore(system, encode_rows(rows))

    assert refused.value.messages == [
        f"Export line {line} does not hold a succeeded live job: {field}."
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


def test_a_live_acquisition_without_intervals_keeps_its_missing_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    empty = next(row for row in export_rows(before.content) if row["id"] == before.lookback.pk)
    empty.update(normalized_intervals=[], coverage_start=None, coverage_end=None)

    summary = restore(system, encode_rows([empty]))

    assert outcomes(summary) == {"estimated_actuals": {"restored": 1}}
    assert [item.period for item in summary.endpoints] == [None]
    assert summary.days == ()
    copy = restored_copy(system.binding, before.lookback)
    assert (copy.normalized_intervals, copy.coverage_start, copy.coverage_end) == ([], None, None)


def test_a_declared_installation_date_matches_the_same_exported_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    SiteBinding.objects.filter(pk=system.binding.pk).update(installation_date=date(2025, 3, 1))
    system.binding.refresh_from_db()
    before = collections_before_reset(system)
    assert {row["binding"]["installation_date"] for row in export_rows(before.content)} == {
        "2025-03-01"
    }

    assert restore(system, before.content).restored == 8


def test_restored_actuals_request_calibration_preparation_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = restore_system(monkeypatch)
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        PlantParameters(name="Plant", dc_capacity_mw=1),
    )
    before = collections_before_reset(system)
    observed: list[int] = []

    def prepare(plant_id: int, now: datetime | None = None) -> None:
        assert restored_count(system) == 8
        observed.append(plant_id)

    monkeypatch.setattr("pyracast.plants.preparation.prepare_calibration", prepare)
    forecasts = encode_rows(
        [row for row in export_rows(before.content) if row["endpoint"] == "forecasts"]
    )

    restore(system, before.content, dry_run=True)
    restore(system, forecasts)
    assert observed == []
    restore(system, before.content)
    assert observed == [plant.pk]
    assert not ForecastIssuance.objects.exists()


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

    assert results == [0, 8]
    assert restored_count(system) == 8


def command_arguments(
    system: System, export: str, *, username: str = "admin", binding_id: int | None = None
) -> list[str]:
    return [
        "--username",
        username,
        "--tenant-id",
        str(system.tenant.pk),
        "--region-id",
        str(system.region.pk),
        "--binding-id",
        str(system.binding.pk if binding_id is None else binding_id),
        "--export",
        export,
    ]


def run_command(*arguments: str) -> str | int | None:
    """Run the command as the `pyracast` executable does, printing to the real streams."""
    try:
        Command().run_from_argv(
            ["pyracast", "restore_provider_snapshots", "--skip-checks", *arguments]
        )
    except SystemExit as exited:
        return exited.code
    return 0


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
            f"Provider snapshot restore for binding {system.binding.pk}: 10 export rows, "
            f"SHA-256 {digest}.",
            f"estimated_actuals: 5 {verb}; covers "
            "2026-10-29T07:00:00+00:00 to 2026-11-07T21:00:00+00:00.",
            f"forecasts: 3 {verb}; covers 2026-11-04T21:00:00+00:00 to 2026-11-08T21:00:00+00:00.",
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
    assert output.getvalue().splitlines() == report("restored", "Restored 8 snapshots.")

    output = StringIO()
    call_command("restore_provider_snapshots", *command_arguments(system, str(path)), stdout=output)
    assert output.getvalue().splitlines()[1:3] == [
        "estimated_actuals: 5 already restored; covers "
        "2026-10-29T07:00:00+00:00 to 2026-11-07T21:00:00+00:00.",
        "forecasts: 3 already restored; covers "
        "2026-11-04T21:00:00+00:00 to 2026-11-08T21:00:00+00:00.",
    ]
    assert output.getvalue().splitlines()[-1] == "Nothing to restore."

    messages = [
        record.getMessage() for record in pyracast_logs.records if record.name == COMMAND_LOGGER
    ]
    assert messages == [
        f"provider_snapshot_restore_finished binding_id={system.binding.pk} dry_run={dry_run} "
        f"restored={restored} skipped={skipped} export_sha256={digest} accepted="
        for dry_run, restored, skipped in ((True, 8, 2), (False, 8, 2), (False, 0, 10))
    ]
    assert SITE not in pyracast_logs.text
    assert KEY not in pyracast_logs.text


def test_command_reports_forecasts_alone_without_a_day_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    path = tmp_path / "forecasts.jsonl.gz"
    path.write_bytes(
        encode_rows([row for row in export_rows(before.content) if row["endpoint"] == "forecasts"])
    )
    output = StringIO()

    call_command(
        "restore_provider_snapshots",
        *command_arguments(system, str(path)),
        "--dry-run",
        stdout=output,
    )

    assert output.getvalue().splitlines()[1:] == [
        "forecasts: 3 to restore; covers 2026-11-04T21:00:00+00:00 to 2026-11-08T21:00:00+00:00.",
        "Dry run: nothing was written.",
    ]


@pytest.fixture
def detached_console_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging.getLogger("pyracast"), "handlers", [])


def test_command_prints_a_fixed_sentence_per_refusal_and_no_export_value_or_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    pyracast_logs: pytest.LogCaptureFixture,
    detached_console_handler: None,
) -> None:
    system = restore_system(monkeypatch)
    before = collections_before_reset(system)
    folder = tmp_path / SITE
    folder.mkdir()
    path = folder / f"{SITE}.solcast.jsonl.gz"
    path.write_bytes(before.content)
    foreign = binding_before_reset(system)
    capsys.readouterr()

    assert run_command(*command_arguments(system, str(path)), "--dry-run") == 0
    printed = capsys.readouterr()
    assert printed.err == ""
    assert printed.out.splitlines()[-1] == "Dry run: nothing was written."
    assert SITE not in printed.out

    rows = export_rows(before.content)
    rows[0]["binding"]["capacity_kw"] = 5
    path.write_bytes(encode_rows(rows))
    refusals = [
        (command_arguments(system, str(folder / "missing.jsonl.gz")), "The export cannot be read."),
        (command_arguments(system, str(path), username="unknown"), "The user does not exist."),
        (
            command_arguments(system, str(path), username=system.member.username),
            "The user is not a platform administrator.",
        ),
        (
            command_arguments(system, str(path), binding_id=foreign.pk),
            "The region or binding does not exist in that tenant.",
        ),
        (
            command_arguments(system, str(path)),
            "The export's provider site differs from the binding in: capacity_kw.",
        ),
    ]
    for arguments, sentence in refusals:
        assert run_command(*arguments) == 1
        assert capsys.readouterr() == (
            "",
            f"CommandError: Provider snapshot restore failed: {sentence}\n",
        )

    assert restored_count(system) == 0
    assert [
        record.getMessage() for record in pyracast_logs.records if record.name == COMMAND_LOGGER
    ] == [
        f"provider_snapshot_restore_finished binding_id={system.binding.pk} dry_run=True "
        f"restored=8 skipped=2 export_sha256={sha256(before.content).hexdigest()} accepted=",
        *(
            f"provider_snapshot_restore_refused binding_id={binding_id} error_type={name}"
            for binding_id, name in (
                (system.binding.pk, "FileNotFoundError"),
                (system.binding.pk, "DoesNotExist"),
                (system.binding.pk, "ForbiddenError"),
                (foreign.pk, "DoesNotExist"),
                (system.binding.pk, "RestoreRefusedError"),
            )
        ),
    ]
    assert SITE not in pyracast_logs.text


def test_command_prints_an_unexpected_failure_by_type_and_origin_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    pyracast_logs: pytest.LogCaptureFixture,
    detached_console_handler: None,
) -> None:
    system = restore_system(monkeypatch)
    path = tmp_path / f"{SITE}.solcast.jsonl.gz"
    path.write_bytes(collections_before_reset(system).content)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"{SITE} in {path}")

    monkeypatch.setattr(f"{COMMAND_LOGGER}.restore_provider_snapshots", fail)
    capsys.readouterr()

    assert run_command(*command_arguments(system, str(path))) == 1

    printed = capsys.readouterr()
    origin = re.escape(COMMAND_LOGGER) + r":\d+:handle"
    assert printed.out == ""
    assert re.fullmatch(
        "CommandError: Provider snapshot restore failed unexpectedly: "
        rf"RuntimeError at {origin}\.\n",
        printed.err,
    )
    [message] = [
        record.getMessage() for record in pyracast_logs.records if record.name == COMMAND_LOGGER
    ]
    assert re.fullmatch(
        f"provider_snapshot_restore_failed binding_id={system.binding.pk} "
        rf"error_type=RuntimeError origin={origin}",
        message,
    )
    assert SITE not in printed.err + pyracast_logs.text
    assert str(tmp_path) not in printed.err + pyracast_logs.text


def test_command_accepts_a_recorded_difference_by_name_and_reports_it_on_every_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system = restore_system(monkeypatch)
    before = installation_date_recorded_differently(system)
    digest = sha256(before.content).hexdigest()
    path = tmp_path / "export.solcast.jsonl.gz"
    path.write_bytes(before.content)
    arguments = command_arguments(system, str(path))
    accept = ("--accept-recorded-difference", "installation_date")

    with pytest.raises(CommandError) as refused:
        call_command("restore_provider_snapshots", *arguments, stdout=StringIO())
    assert str(refused.value) == (
        "Provider snapshot restore failed: "
        "The export's provider site differs from the binding in: installation_date."
    )

    def stored() -> tuple[int, int, int]:
        return (
            CollectionJob.objects.count(),
            ForecastSnapshot.objects.count(),
            ForecastResult.objects.count(),
        )

    reports = []
    for options in (("--dry-run", *accept), accept, (*accept, *accept)):
        before_run = stored()
        output = StringIO()
        call_command("restore_provider_snapshots", *arguments, *options, stdout=output)
        reports.append(output.getvalue().splitlines())
    assert stored() == before_run

    for report, outcome in zip(
        reports,
        ("Dry run: nothing was written.", "Restored 8 snapshots.", "Nothing to restore."),
        strict=True,
    ):
        assert report[0].startswith(f"Provider snapshot restore for binding {system.binding.pk}")
        assert report[1] == ACCEPTED_LINE
        assert report[-1] == outcome
        assert sum(ACCEPTED_LINE in line for line in report) == 1
    assert restored_count(system) == 8
    assert [
        record.getMessage() for record in pyracast_logs.records if record.name == COMMAND_LOGGER
    ] == [
        f"provider_snapshot_restore_refused binding_id={system.binding.pk} "
        "error_type=RestoreRefusedError",
        *(
            f"provider_snapshot_restore_finished binding_id={system.binding.pk} "
            f"dry_run={dry_run} restored={restored} skipped={skipped} export_sha256={digest} "
            "accepted=installation_date"
            for dry_run, restored, skipped in ((True, 8, 2), (False, 8, 2), (False, 0, 10))
        ),
    ]
    assert "2025-03" not in pyracast_logs.text


def test_command_reports_every_exported_value_of_an_accepted_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system = restore_system(monkeypatch)
    before = installation_date_recorded_differently(system)
    rows = export_rows(before.content)
    next(row for row in rows if row["id"] == before.nights[1].pk)["binding"][
        "installation_date"
    ] = None
    path = tmp_path / "export.solcast.jsonl.gz"
    path.write_bytes(encode_rows(rows))
    output = StringIO()

    call_command(
        "restore_provider_snapshots",
        *command_arguments(system, str(path)),
        "--dry-run",
        "--accept-recorded-difference",
        "installation_date",
        stdout=output,
    )

    assert output.getvalue().splitlines()[1] == (
        'Accepted recorded difference in installation_date: export "2025-03-01", null; '
        'binding "2025-03-04".'
    )


def test_command_never_accepts_a_setting_the_export_lacks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system = restore_system(monkeypatch)
    before = installation_date_recorded_differently(system)
    rows = export_rows(before.content)
    del next(row for row in rows if row["id"] == before.nights[1].pk)["binding"][
        "installation_date"
    ]
    path = tmp_path / "export.solcast.jsonl.gz"
    path.write_bytes(encode_rows(rows))

    with pytest.raises(CommandError) as refused:
        call_command(
            "restore_provider_snapshots",
            *command_arguments(system, str(path)),
            "--accept-recorded-difference",
            "installation_date",
            stdout=StringIO(),
        )

    assert str(refused.value) == (
        "Provider snapshot restore failed: "
        "The export's provider site differs from the binding in: installation_date."
    )
    assert restored_count(system) == 0


def test_command_refuses_an_accepted_setting_that_does_not_differ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system = restore_system(monkeypatch)
    path = tmp_path / "export.solcast.jsonl.gz"
    path.write_bytes(collections_before_reset(system).content)

    with pytest.raises(CommandError) as refused:
        call_command(
            "restore_provider_snapshots",
            *command_arguments(system, str(path)),
            "--dry-run",
            "--accept-recorded-difference",
            "installation_date",
            stdout=StringIO(),
        )

    assert str(refused.value) == (
        "Provider snapshot restore failed: "
        "The export's provider site does not differ from the binding in: installation_date."
    )


@pytest.mark.parametrize("name", ["capacity_kw", "site_id", "timezone"])
def test_command_accepts_only_a_recorded_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    system = restore_system(monkeypatch)
    path = tmp_path / "export.solcast.jsonl.gz"
    path.write_bytes(installation_date_recorded_differently(system).content)

    with pytest.raises(CommandError) as refused:
        call_command(
            "restore_provider_snapshots",
            *command_arguments(system, str(path)),
            "--dry-run",
            "--accept-recorded-difference",
            name,
            stdout=StringIO(),
        )

    assert re.fullmatch(
        rf"Error: argument --accept-recorded-difference: invalid choice: '?{name}'? "
        r"\(choose from '?installation_date'?\)",
        str(refused.value),
    )
    assert restored_count(system) == 0
