from datetime import timedelta
from typing import Any

import pytest
from django.db import IntegrityError, transaction

from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasConnection
from pyracast.plants.measurements import (
    MEASUREMENT_POLICY,
    SETTLEMENT_ONLY_MEASUREMENT_POLICY,
    latest_measurement_day,
    production_identity_current,
    select_measurements,
    selection_fingerprint,
)
from pyracast.plants.models import CalibrationRun, PlantConfiguration, ProductionObservation
from tests.support.domain import NOW
from tests.support.provisional_measurements import DAY, acquisition, hourly_readings
from tests.support.provisional_measurements import source as source

pytestmark = pytest.mark.django_db(transaction=True)


def test_hour_precedence_preserves_zero_fallback_and_manual_quality(
    source: tuple[PlantConfiguration, EpiasConnection],
) -> None:
    configuration, connection = source
    realtime = acquisition(source, values=["9"] * 6)
    settlement = acquisition(
        source, "settlement_injection", values=["0", None, "-1", "bad", "4", "5"]
    )
    start, end = local_day_bounds(DAY, configuration.binding.timezone)
    manual = ProductionObservation.objects.create(
        plant=configuration.plant,
        configuration=configuration,
        starts_at=start + timedelta(hours=4, minutes=30),
        ends_at=start + timedelta(hours=5),
        energy_mwh=1,
        quality="invalid",
        source="manual",
        recorded_by=connection.connected_by,
    )
    rows, ids = select_measurements(configuration, start, end, NOW)
    assert [(row.energy_mwh, row.source, row.quality) for row in rows] == [
        (0, "settlement_injection", "valid"),
        (9, "realtime_generation", "valid"),
        (9, "realtime_generation", "valid"),
        (9, "realtime_generation", "valid"),
        (1, "manual", "invalid"),
        (5, "settlement_injection", "valid"),
    ]
    assert set(ids) == {realtime.pk, settlement.pk}
    assert rows[4].pk == manual.pk
    assert all(row.ends_at - row.starts_at == timedelta(hours=1) for row in rows if row.snapshot)
    assert latest_measurement_day(configuration, end, NOW, manual.pk) == DAY


def test_latest_per_source_never_backfills_an_older_acquisition(
    source: tuple[PlantConfiguration, EpiasConnection],
) -> None:
    configuration, _ = source
    old = acquisition(source, values=["20"] * 24)
    latest = acquisition(source, minute=1, values=["1", None])
    old_settlement = acquisition(source, "settlement_injection", values=["30"] * 24)
    acquisition(source, "settlement_injection", minute=1, values=[None, "2", "3"])
    start, end = local_day_bounds(DAY, configuration.binding.timezone)
    rows, ids = select_measurements(configuration, start, end, NOW)
    assert [row.energy_mwh for row in rows] == [1, 2, 3]
    assert old.pk not in ids
    assert old_settlement.pk not in ids
    assert latest.pk in ids
    assert len(rows) == 3


def test_equal_energy_settlement_replaces_provisional_fingerprint(
    source: tuple[PlantConfiguration, EpiasConnection],
) -> None:
    configuration, _ = source
    acquisition(source)
    start, end = local_day_bounds(DAY, configuration.binding.timezone)
    provisional, _ = select_measurements(configuration, start, end, NOW)
    assert len(provisional) == 24
    before = selection_fingerprint(configuration, provisional, NOW)[0]
    acquisition(source, minute=1)
    repeated, _ = select_measurements(configuration, start, end, NOW)
    assert selection_fingerprint(configuration, repeated, NOW)[0] == before
    acquisition(source, "settlement_injection")
    settled, _ = select_measurements(configuration, start, end, NOW)
    assert selection_fingerprint(configuration, settled, NOW)[0] != before


@pytest.mark.parametrize("field", ["collected_at", "available_at"])
def test_provider_acquisition_cutoff_rejects_future_vintage(
    source: tuple[PlantConfiguration, EpiasConnection], field: str
) -> None:
    configuration, _ = source
    old = acquisition(source, values=["1"])
    future: dict[str, Any] = {field: NOW + timedelta(seconds=1)}
    acquisition(source, minute=1, values=["2"], **future)
    start, end = local_day_bounds(DAY, configuration.binding.timezone)
    rows, ids = select_measurements(configuration, start, end, NOW)
    assert ids == [old.pk]
    assert [row.energy_mwh for row in rows] == [1]


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_plant": {"id": 77, "eic": "EIC"}},
        {"provider_plant": {"id": 2, "eic": "OTHER"}},
        {"normalization_policy": "unknown"},
    ],
)
def test_provisional_rejects_wrong_identity_or_unknown_normalization(
    source: tuple[PlantConfiguration, EpiasConnection], changes: dict[str, Any]
) -> None:
    configuration, _ = source
    acquisition(source, **changes)
    start, end = local_day_bounds(DAY, configuration.binding.timezone)
    assert select_measurements(configuration, start, end, NOW) == ([], [])


def test_native_hours_and_missingness_are_preserved(
    source: tuple[PlantConfiguration, EpiasConnection],
) -> None:
    configuration, _ = source
    readings = hourly_readings(["0", "NaN", "Infinity", "-1", None])
    valid = readings[0]
    snapshot = acquisition(
        source,
        readings=[
            *readings,
            None,
            {**valid, "reported": False, "total_mwh": "90"},
            {**valid, "period_start": "2026-11-01T00:00:00"},
            {**valid, "period_start": "2026-11-01T00:30:00+03:00"},
            {**valid, "period_start": "bad"},
        ],
    )
    start, end = local_day_bounds(DAY, configuration.binding.timezone)
    rows, ids = select_measurements(configuration, start, end, NOW)
    assert ids == [snapshot.pk]
    assert [(row.starts_at, row.ends_at, row.energy_mwh) for row in rows] == [
        (start, start + timedelta(hours=1), 0)
    ]
    assert select_measurements(configuration, start, end, start) == ([], [])


def test_pinned_sources_replay_and_settlement_only_policy_never_adopts_provisional(
    source: tuple[PlantConfiguration, EpiasConnection],
) -> None:
    configuration, _ = source
    realtime = acquisition(source, values=["1", "2"])
    settlement = acquisition(source, "settlement_injection", values=["5", None])
    start, end = local_day_bounds(DAY, configuration.binding.timezone)
    original, ids = select_measurements(configuration, start, end, NOW)
    acquisition(source, minute=1, values=["9", "9"])
    acquisition(source, "settlement_injection", minute=1, values=["7", "7"])
    replay, pinned = select_measurements(
        configuration, start, end, NOW, production_snapshot_ids=ids
    )
    assert [row.energy_mwh for row in original] == [5, 2]
    assert replay == original
    assert set(pinned) == {realtime.pk, settlement.pk}
    assert select_measurements(configuration, start, end, NOW, production_snapshot_ids=[]) == (
        [],
        [],
    )
    settled, settled_ids = select_measurements(
        configuration,
        start,
        end,
        NOW,
        production_snapshot_ids=ids,
        policy=SETTLEMENT_ONLY_MEASUREMENT_POLICY,
    )
    assert settled_ids == [settlement.pk]
    assert [row.energy_mwh for row in settled] == [5]
    assert (
        selection_fingerprint(configuration, settled, NOW)[0]
        != selection_fingerprint(
            configuration, settled, NOW, policy=SETTLEMENT_ONLY_MEASUREMENT_POLICY
        )[0]
    )


@pytest.mark.parametrize("change", ["eic", "provider_plant_id", "job_kind", "normalization"])
def test_provisional_identity_guard_checks_service_specific_identity(
    source: tuple[PlantConfiguration, EpiasConnection], change: str
) -> None:
    configuration, connection = source
    snapshot = acquisition(source)
    run = CalibrationRun(
        plant=configuration.plant,
        configuration=configuration,
        production_snapshot_ids=[snapshot.pk],
        measurement_selection_policy=MEASUREMENT_POLICY,
    )
    assert production_identity_current(run)
    connection.enabled = False
    connection.save(update_fields=("enabled",))
    assert production_identity_current(run)
    if change in {"eic", "provider_plant_id"}:
        setattr(connection, change, "OTHER" if change == "eic" else 3)
        connection.save(update_fields=(change,))
    elif change == "job_kind":
        snapshot.job.source_kind = "settlement_injection"
        snapshot.job.save(update_fields=("source_kind",))
    else:
        unknown = acquisition(source, minute=1, normalization_policy="unknown")
        run.production_snapshot_ids = [unknown.pk]
    assert not production_identity_current(run)
    run.production_snapshot_ids = []
    assert production_identity_current(run)


def test_provisional_selection_requires_enabled_net_export_connection(
    source: tuple[PlantConfiguration, EpiasConnection],
) -> None:
    configuration, connection = source
    snapshot = acquisition(source)
    connection.settlement_plant_id = None
    connection.save(update_fields=("settlement_plant_id",))
    start, end = local_day_bounds(DAY, configuration.binding.timezone)
    assert select_measurements(configuration, start, end, NOW)[1] == [snapshot.pk]
    assert select_measurements(
        configuration, start, end, NOW, policy=SETTLEMENT_ONLY_MEASUREMENT_POLICY
    ) == ([], [])
    configuration.measurement_basis = "ac_generation"
    assert select_measurements(configuration, start, end, NOW) == ([], [])
    configuration.measurement_basis = "net_export"
    connection.enabled = False
    connection.save(update_fields=("enabled",))
    assert select_measurements(configuration, start, end, NOW) == ([], [])


def test_invalid_job_source_and_unknown_policy_are_rejected(
    source: tuple[PlantConfiguration, EpiasConnection],
) -> None:
    configuration, _ = source
    snapshot = acquisition(source)
    snapshot.job.source_kind = "settlement_injection"
    snapshot.job.save(update_fields=("source_kind",))
    start, end = local_day_bounds(DAY, configuration.binding.timezone)
    assert select_measurements(configuration, start, end, NOW) == ([], [])
    with pytest.raises(ValueError, match="Unknown measurement"):
        select_measurements(configuration, start, end, NOW, policy="unknown")


def test_latest_day_includes_provisional_but_only_completed_local_dates(
    source: tuple[PlantConfiguration, EpiasConnection],
) -> None:
    configuration, _ = source
    acquisition(source)
    start, end = local_day_bounds(DAY, configuration.binding.timezone)
    assert latest_measurement_day(configuration, end, NOW, 0) == DAY
    assert latest_measurement_day(configuration, end - timedelta(minutes=1), NOW, 0) is None
    assert (
        latest_measurement_day(
            configuration, end, NOW, 0, policy=SETTLEMENT_ONLY_MEASUREMENT_POLICY
        )
        is None
    )
    assert select_measurements(configuration, start + timedelta(minutes=30), end, NOW)[0][
        0
    ].starts_at == start + timedelta(hours=1)


@pytest.mark.parametrize("quality", ["valid", "invalid", "outage", "curtailed"])
def test_any_manual_quality_suppresses_provider_hour_with_pinned_watermark(
    source: tuple[PlantConfiguration, EpiasConnection], quality: str
) -> None:
    configuration, connection = source
    snapshot = acquisition(source, values=["9"])
    start, end = local_day_bounds(DAY, configuration.binding.timezone)
    manual = ProductionObservation.objects.create(
        plant=configuration.plant,
        configuration=configuration,
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        energy_mwh=1,
        quality=quality,
        source="manual",
        recorded_by=connection.connected_by,
    )
    rows, ids = select_measurements(configuration, start, end, NOW)
    assert ids == []
    assert [(row.energy_mwh, row.quality, row.source) for row in rows] == [(1, quality, "manual")]
    before_manual, ids = select_measurements(
        configuration, start, end, NOW, observation_id_cutoff=manual.pk - 1
    )
    assert ids == [snapshot.pk]
    assert [row.energy_mwh for row in before_manual] == [9]


@pytest.mark.parametrize("boundary", ["snapshot", "job"])
def test_acquisitions_cannot_cross_configuration_boundaries(
    source: tuple[PlantConfiguration, EpiasConnection], boundary: str
) -> None:
    configuration, _ = source
    snapshot = acquisition(source)
    other = PlantConfiguration.objects.create(
        region_id=configuration.plant.region_id,
        plant=configuration.plant,
        binding=configuration.binding,
        revision=2,
        dc_capacity_mw=130,
        measurement_basis="net_export",
        created_by=configuration.created_by,
    )
    with pytest.raises(IntegrityError), transaction.atomic():  # noqa: PT012 -- validate before commit.
        if boundary == "snapshot":
            snapshot.configuration = other
            snapshot.save(update_fields=("configuration",))
        else:
            snapshot.job.configuration = other
            snapshot.job.save(update_fields=("configuration",))
        start, end = local_day_bounds(DAY, configuration.binding.timezone)
        assert select_measurements(configuration, start, end, NOW) == ([], [])
        assert select_measurements(
            configuration, start, end, NOW, production_snapshot_ids=[snapshot.pk]
        ) == ([], [])
