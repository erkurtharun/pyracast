from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from threading import Barrier
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction

from pyracast.accounts.models import Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.capacity_normalization import (
    _scaled_intervals,
    normalize_reference_capacity,
)
from pyracast.forecasts.history import select_actual_snapshot
from pyracast.forecasts.models import (
    ForecastSnapshot,
    HistoricalReferenceImport,
    ReferenceCapacityNormalization,
)
from pyracast.forecasts.trust import normalization_problems
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import Plant, PlantConfiguration, ProductionObservation
from pyracast.plants.preparation import prepare_calibration
from pyracast.plants.training import run_calibration
from pyracast.regions.models import AuditEvent, Region
from pyracast.trash import RETENTION, purge_expired, trash_region
from tests.support.capacity_normalization import normalize, scaled_system
from tests.support.domain import NOW
from tests.support.invariants import stored_again
from tests.support.plant_operations import publish_snapshot, snapshot_for
from tests.support.plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("publication", ["missing", "future"])
def test_unknown_or_future_publication_does_not_unlock_a_legacy_forecast(
    monkeypatch: pytest.MonkeyPatch,
    publication: str,
) -> None:
    system, target = scaled_system(monkeypatch)
    source = publish_snapshot(snapshot_for(system, date(2026, 11, 9)))
    source.available_at = None
    stored_again(source)
    result = source.forecastresult
    if publication == "missing":
        result.delete()
    else:
        result.published_at = NOW + timedelta(seconds=1)
        result.save()
    assert normalize(system, target) == ()
    assert not ReferenceCapacityNormalization.objects.exists()


def test_historical_exports_cannot_be_reclassified_as_native_by_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    source = actual_snapshot_for(system, date(2026, 11, 7))
    source.job.reference_import = HistoricalReferenceImport.objects.create(
        binding=system.binding,
        content_sha256="a" * 64,
        source_name="history.json",
        source_settings={"product": "historical_rooftop_pv"},
        imported_by=system.admin,
        created_at=NOW,
        interval_count=48,
    )
    source.job.endpoint = "historical_import"
    source.job.save()
    assert normalize(system, target) == ()


def test_scaled_copies_cannot_be_normalized_again(monkeypatch: pytest.MonkeyPatch) -> None:
    system, target = scaled_system(monkeypatch)
    actual_snapshot_for(system, date(2026, 11, 7))
    normalize(system, target)
    next_target = SiteBinding.objects.get(pk=target.pk)
    next_target.pk = None
    next_target.capacity_kw = next_target.dc_capacity_kw = 1_000_000
    next_target.region_revision += 1
    next_target.save()
    system.region.approved_binding = next_target
    system.region.revision = next_target.region_revision
    system.region.save(update_fields=["approved_binding", "revision"])
    assert normalize(replace(system, binding=target), next_target) == ()
    assert ReferenceCapacityNormalization.objects.count() == 1


@pytest.mark.parametrize(
    ("side", "patch"),
    [
        ("source", {"capacity_semantics_verified": False}),
        ("target", {"settings_verification_reference": ""}),
        ("target", {"capacity_kw": 1}),
        ("source", {"dc_capacity_kw": 2}),
        ("source", {"capacity_kw": 1e-308, "dc_capacity_kw": 1e-308}),
    ],
)
def test_non_equivalent_or_unverified_capacity_declarations_cannot_relabel_history(
    monkeypatch: pytest.MonkeyPatch, side: str, patch: dict[str, Any]
) -> None:
    system, target = scaled_system(monkeypatch)
    binding = system.binding if side == "source" else target
    SiteBinding.objects.filter(pk=binding.pk).update(**patch)
    with pytest.raises(ValidationError):
        normalize(system, target)
    assert not ReferenceCapacityNormalization.objects.exists()


def test_legacy_nonfinite_binding_cannot_authorize_capacity_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    system.binding.capacity_kw = float("inf")
    assert "capacity_invalid" in normalization_problems(system.binding, target)
    assert not ReferenceCapacityNormalization.objects.exists()


def test_same_binding_and_foreign_tenant_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    system, target = scaled_system(monkeypatch)
    with pytest.raises(ValidationError):
        normalize_reference_capacity(
            system.admin,
            system.tenant.pk,
            system.region.pk,
            target.pk,
            target.pk,
            "Same site",
        )
    foreign = Tenant.objects.create(name="Foreign tenant")
    with pytest.raises(Region.DoesNotExist):
        normalize_reference_capacity(
            system.admin,
            foreign.pk,
            system.region.pk,
            system.binding.pk,
            target.pk,
            "Same site",
        )
    with pytest.raises(SiteBinding.DoesNotExist):
        normalize_reference_capacity(
            system.admin,
            system.tenant.pk,
            system.region.pk,
            target.pk + 999,
            target.pk,
            "Same site",
        )


def test_known_dc_capacity_cannot_be_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    system, target = scaled_system(monkeypatch)
    SiteBinding.objects.filter(pk=target.pk).update(dc_capacity_kw=None)
    with pytest.raises(ValidationError):
        normalize(system, target)


def test_undeclared_source_dc_capacity_cannot_be_scaled(monkeypatch: pytest.MonkeyPatch) -> None:
    # The provider applies an implicit DC/AC ratio when DC capacity is undeclared.
    system, target = scaled_system(monkeypatch)
    SiteBinding.objects.filter(pk=system.binding.pk).update(dc_capacity_kw=None)
    with pytest.raises(ValidationError):
        normalize(system, target)


@pytest.mark.parametrize("power", [True, "0.6", -1, 1e308])
def test_invalid_power_rolls_back_the_entire_normalization(
    monkeypatch: pytest.MonkeyPatch, power: object
) -> None:
    system, target = scaled_system(monkeypatch)
    actual_snapshot_for(system, date(2026, 11, 6))
    invalid = actual_snapshot_for(system, date(2026, 11, 7))
    invalid.normalized_intervals[0]["power_kw"] = power
    stored_again(invalid)
    with pytest.raises(ValidationError):
        normalize(system, target)
    assert not ForecastSnapshot.objects.filter(job__binding=target).exists()
    assert not ReferenceCapacityNormalization.objects.exists()
    assert not AuditEvent.objects.filter(action="reference_capacity_normalized").exists()


def test_finite_source_power_cannot_overflow_when_scaling() -> None:
    with pytest.raises(ValidationError, match="finite"):
        _scaled_intervals([{"power_kw": 1e308}], 1000)


def test_a_scale_at_the_float_limits_is_stored_as_the_quotient_of_its_capacities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    SiteBinding.objects.filter(pk=system.binding.pk).update(capacity_kw=1e308, dc_capacity_kw=1e308)
    SiteBinding.objects.filter(pk=target.pk).update(capacity_kw=1e-10, dc_capacity_kw=1e-10)
    actual_snapshot_for(system, date(2026, 11, 7))

    assert len(normalize(system, target)) == 1

    stored = ReferenceCapacityNormalization.objects.get()
    assert (stored.source_capacity_kw, stored.target_capacity_kw) == (1e308, 1e-10)
    assert stored.factor == 1e-10 / 1e308


def test_live_native_history_takes_precedence_over_scaled_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    day = date(2026, 11, 7)
    actual_snapshot_for(system, day, power_kw=0.6)
    ids = normalize(system, target)
    native = actual_snapshot_for(replace(system, binding=target), day, power_kw=500)
    assert select_actual_snapshot(target, day, NOW) == native
    native.normalized_intervals.pop()
    stored_again(native)
    selected = select_actual_snapshot(target, day, NOW)
    assert selected is not None
    assert selected.pk == ids[0]


def test_normalization_replays_are_serialized_and_unique_in_postgresql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    actual_snapshot_for(system, date(2026, 11, 7))
    barrier = Barrier(2)

    def invoke() -> tuple[int, ...]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return normalize(system, target)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: invoke(), range(2)))
    assert results[0] == results[1]
    assert ReferenceCapacityNormalization.objects.count() == 1
    assert AuditEvent.objects.filter(action="reference_capacity_normalized").count() == 1
    record = ReferenceCapacityNormalization.objects.get()
    record.pk = None
    record.snapshot = record.source_snapshot
    with pytest.raises(IntegrityError), transaction.atomic():
        record.save(force_insert=True)


def test_normalized_hobby_days_enter_current_calibration_with_original_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    plant = Plant.objects.get(region=system.region)
    configuration_id = plant.current_configuration_id
    assert configuration_id is not None
    PlantConfiguration.objects.filter(pk=configuration_id).update(binding=target)
    for offset in range(7):
        day = date(2026, 10, 25) + timedelta(days=offset)
        actual_snapshot_for(system, day)
        start, _ = local_day_bounds(day, target.timezone)
        ProductionObservation.objects.bulk_create(
            [
                ProductionObservation(
                    plant=plant,
                    configuration_id=configuration_id,
                    starts_at=start + timedelta(hours=hour),
                    ends_at=start + timedelta(hours=hour + 1),
                    energy_mwh=26,
                    quality="valid",
                    source="meter",
                    recorded_by=system.member,
                )
                for hour in range(24)
            ]
        )
    normalized_ids = normalize(system, target)
    run = prepare_calibration(plant.pk)
    assert run is not None
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "ready"
    assert run.result["valid_days"] == 7
    validation = run.result["current_source_validation"]
    assert validation["valid_days"] == 7
    assert validation["native_valid_days"] == 0
    assert validation["capacity_normalized_valid_days"] == 7
    assert not validation["eligible"]
    assert {source["snapshot_id"] for source in run.dataset_manifest["sources"].values()} == set(
        normalized_ids
    )
    assert all(
        source["capacity_normalization"]["source_capacity_kw"] == 1
        for source in run.dataset_manifest["sources"].values()
    )


def test_normalization_records_do_not_block_authorized_region_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    actual_snapshot_for(system, date(2026, 11, 7))
    normalize(system, target)
    trash_region(system.member, system.tenant.pk, system.region.pk)
    assert purge_expired(NOW + RETENTION) == {"regions": 1, "plants": 1}
    assert not ReferenceCapacityNormalization.objects.exists()
