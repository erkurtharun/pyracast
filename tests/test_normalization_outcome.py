from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection
from django.test.utils import CaptureQueriesContext
from psycopg.errors import LockNotAvailable

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.capacity_normalization import normalize_reference_capacity
from pyracast.forecasts.management.commands import normalize_reference_capacity as command
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
    ReferenceCapacityNormalization,
)
from pyracast.regions.models import AuditEvent, Region
from tests.support.capacity_normalization import normalize, scaled_system
from tests.support.domain import System
from tests.support.plant_operations import publish_snapshot, snapshot_for
from tests.support.plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
REFERENCE = "User confirms the same Hobby model with only a 1 to 1000 kW reference scale change"


def run(system: System, *options: str) -> str:
    output = StringIO()
    call_command(
        "normalize_reference_capacity",
        "--username",
        system.admin.username,
        "--tenant-id",
        str(system.tenant.pk),
        "--region-id",
        str(system.region.pk),
        "--source-binding-id",
        str(system.binding.pk),
        "--reference",
        REFERENCE,
        *options,
        stdout=output,
    )
    return output.getvalue()


def rows() -> tuple[int, int, int, int, int]:
    return (
        CollectionJob.objects.count(),
        ForecastSnapshot.objects.count(),
        ForecastResult.objects.count(),
        ReferenceCapacityNormalization.objects.count(),
        AuditEvent.objects.filter(action="reference_capacity_normalized").count(),
    )


def history(system: System, days: int) -> None:
    for offset in range(days):
        actual_snapshot_for(system, date(2026, 10, 1) + timedelta(days=offset), power_kw=0.6)


def test_the_command_normalizes_onto_the_approved_binding_and_reports_what_it_wrote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    actual_snapshot_for(system, date(2026, 11, 7), power_kw=0.6)
    publish_snapshot(snapshot_for(system, date(2026, 11, 9), power_kw=0.6))

    output = run(system)

    lines = output.splitlines()
    assert lines[0] == (
        f"Source binding {system.binding.pk}: 1 kW; target binding {target.pk}: 1000 kW; "
        "factor 1000."
    )
    assert lines[1] == (
        "Created 2 snapshots (estimated_actuals 1, forecasts 1) for 2026-11-07 to 2026-11-09; "
        "reused 0."
    )
    assert ReferenceCapacityNormalization.objects.filter(target_binding=target).count() == 2
    audit = AuditEvent.objects.get(action="reference_capacity_normalized")
    assert (audit.actor, audit.region, audit.revision) == (
        system.admin,
        system.region,
        system.region.revision,
    )


def test_a_dry_run_reports_the_plan_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _ = scaled_system(monkeypatch)
    actual_snapshot_for(system, date(2026, 11, 7), power_kw=0.6)
    before = rows()

    output = run(system, "--dry-run")

    assert "Dry run: would create 1 snapshots (estimated_actuals 1)" in output
    assert rows() == before


def test_a_replay_writes_nothing_and_costs_the_same_however_long_the_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    history(system, 2)
    first = normalize(system, target)
    written = rows()
    with CaptureQueriesContext(connection) as short:
        assert normalize(system, target) == first
    history(system, 8)
    normalize(system, target)
    longer = rows()

    with CaptureQueriesContext(connection) as long:
        normalize(system, target)

    assert rows() == longer
    assert written[3] == 2
    assert len(long.captured_queries) == len(short.captured_queries)


def test_forecast_copies_carry_daily_results_and_actuals_copies_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    actual = actual_snapshot_for(system, date(2026, 11, 7), power_kw=0.6)
    forecast = publish_snapshot(snapshot_for(system, date(2026, 11, 9), power_kw=0.6))

    normalize(system, target)

    copies = {
        record.source_snapshot_id: record.snapshot
        for record in ReferenceCapacityNormalization.objects.select_related("snapshot")
    }
    assert not ForecastResult.objects.filter(snapshot=copies[actual.pk]).exists()
    result = ForecastResult.objects.get(snapshot=copies[forecast.pk])
    assert "2026-11-09" in result.daily_payloads


def test_a_source_holding_copies_names_the_binding_that_needs_its_own_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, middle = scaled_system(monkeypatch)
    actual_snapshot_for(system, date(2026, 11, 7), power_kw=0.6)
    normalize(system, middle)
    final = SiteBinding.objects.get(pk=middle.pk)
    final.pk = None
    final.capacity_kw = final.dc_capacity_kw = 2000
    final.region_revision += 1
    final.save()
    system.region.approved_binding = final
    system.region.revision = final.region_revision
    system.region.save(update_fields=["approved_binding", "revision"])
    original = system.binding
    system.binding = middle

    output = run(system)

    assert f"Binding {original.pk} holds copies in the source history" in output


def test_every_refusal_is_printed_on_its_own_line(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _ = scaled_system(monkeypatch)
    SiteBinding.objects.filter(pk=system.binding.pk).update(
        health="error", validated_at=None, validated_credential_revision=None
    )

    with pytest.raises(CommandError) as refusal:
        run(system)

    message = str(refusal.value).splitlines()
    assert message[0] == "Reference capacity normalization failed:"
    assert len(message) == 3


def test_a_busy_region_asks_for_a_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _ = scaled_system(monkeypatch)
    busy = OperationalError("lock timeout")
    busy.__cause__ = LockNotAvailable()

    def locked(*args: object, **kwargs: object) -> None:
        raise busy

    monkeypatch.setattr(command, "normalize_reference_capacity", locked)

    with pytest.raises(CommandError, match="Region busy, retry"):
        run(system)


def test_the_operation_returns_a_typed_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    system, target = scaled_system(monkeypatch)
    actual_snapshot_for(system, date(2026, 11, 7), power_kw=0.6)

    outcome = normalize_reference_capacity(
        system.admin, system.tenant.pk, system.region.pk, system.binding.pk, None, REFERENCE
    )

    assert outcome.target_binding_id == target.pk
    assert outcome.factor == 1000
    assert len(outcome.created) == 1
    assert outcome.reused == ()


def test_a_plant_without_an_approved_binding_names_its_missing_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = scaled_system(monkeypatch)
    Region.objects.filter(pk=system.region.pk).update(status="pending", approved_binding=None)

    with pytest.raises(CommandError, match="no approved connection"):
        run(system)
