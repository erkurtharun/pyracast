from datetime import timedelta
from importlib import import_module
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastSnapshot,
    ReferenceCapacityNormalization,
)
from tests.test_domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_snapshot_binding_backfill_preserves_a_populated_archive() -> None:
    system = make_system()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TABLE connections_sitebinding (id bigint PRIMARY KEY)")
            cursor.execute("INSERT INTO connections_sitebinding VALUES (%s)", (system.binding.pk,))
            cursor.execute(
                "CREATE TEMP TABLE forecasts_collectionjob "
                "(id bigint PRIMARY KEY, binding_id bigint)"
            )
            cursor.execute(
                "CREATE TEMP TABLE forecasts_forecastsnapshot "
                "(id bigint PRIMARY KEY, job_id bigint, "
                "raw_payload jsonb, collected_at timestamptz)"
            )
            cursor.execute(
                "CREATE TEMP TABLE forecasts_referencecapacitynormalization "
                "(id bigint PRIMARY KEY, source_snapshot_id bigint, snapshot_id bigint, "
                "source_capacity_kw double precision, target_capacity_kw double precision)"
            )
            cursor.execute(
                "INSERT INTO forecasts_collectionjob VALUES (1,%s)", (system.binding.pk,)
            )
            cursor.execute(
                "INSERT INTO forecasts_forecastsnapshot VALUES (1,1,%s,%s)",
                ('{"native":true}', NOW),
            )
        name = "0012_forecastsnapshot_binding_and_more"
        migration = import_module(f"pyracast.forecasts.migrations.{name}").Migration(
            name, "forecasts"
        )
        state = MigrationExecutor(connection).loader.project_state(
            [("forecasts", "0011_validate_collection_integrity")]
        )
        with connection.schema_editor() as editor:
            migration.apply(state, editor)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT binding_id, raw_payload = '{\"native\":true}'::jsonb, collected_at "
                "FROM forecasts_forecastsnapshot"
            )
            assert cursor.fetchone() == (system.binding.pk, True, NOW)
        transaction.set_rollback(True)


def test_snapshot_and_normalization_share_the_target_job_binding() -> None:
    system = make_system(approved=True)
    source_job = CollectionJob.objects.get(binding=system.binding)
    source = ForecastSnapshot.objects.create(
        job=source_job,
        binding=system.binding,
        raw_payload={},
        normalized_intervals=[],
        collected_at=NOW,
    )
    target = SiteBinding.objects.get(pk=system.binding.pk)
    target.pk = None
    target.region_revision += 1
    target.capacity_kw = 2
    target.save()
    job = CollectionJob.objects.create(
        binding=target,
        endpoint="scaled_forecasts",
        scheduled_slot=NOW,
        target_date=NOW.date(),
        available_at=NOW,
    )
    with pytest.raises(IntegrityError, match="snapshot_job_binding_fk"), transaction.atomic():
        ForecastSnapshot.objects.create(
            job=job,
            binding=system.binding,
            raw_payload={},
            normalized_intervals=[],
            collected_at=NOW,
        )
    snapshot = ForecastSnapshot.objects.create(
        job=job, binding=target, raw_payload={}, normalized_intervals=[], collected_at=NOW
    )
    values = dict(
        source_snapshot=source,
        snapshot=snapshot,
        source_capacity_kw=1,
        target_capacity_kw=2,
        factor=2,
        authorized_by=system.admin,
        authorization_reference="Authorized fixture conversion",
        created_at=NOW,
    )
    with (
        pytest.raises(IntegrityError, match="normalization_snapshot_binding_fk"),
        transaction.atomic(),
    ):
        ReferenceCapacityNormalization.objects.create(target_binding=system.binding, **values)
    record = ReferenceCapacityNormalization.objects.create(target_binding=target, **values)
    with pytest.raises(IntegrityError, match="normalization_distinct_source"), transaction.atomic():
        ReferenceCapacityNormalization.objects.filter(pk=record.pk).update(source_snapshot=snapshot)
    with pytest.raises(IntegrityError, match="snapshot_job_binding_fk"), transaction.atomic():
        CollectionJob.objects.filter(pk=job.pk).update(binding=system.binding)
    other_job = CollectionJob.objects.create(
        binding=target,
        endpoint="scaled_forecasts",
        scheduled_slot=NOW + timedelta(minutes=1),
        target_date=NOW.date(),
        available_at=NOW,
    )
    other_snapshot = ForecastSnapshot.objects.create(
        job=other_job, binding=target, raw_payload={}, normalized_intervals=[], collected_at=NOW
    )
    values["snapshot"] = other_snapshot
    with (
        pytest.raises(IntegrityError, match="unique_reference_capacity_normalization"),
        transaction.atomic(),
    ):
        ReferenceCapacityNormalization.objects.create(target_binding=target, **values)


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("source_capacity_kw", 0, "normalization_source_capacity"),
        ("source_capacity_kw", -1, "normalization_source_capacity"),
        ("source_capacity_kw", float("inf"), "normalization_source_capacity"),
        ("source_capacity_kw", float("nan"), "normalization_source_capacity"),
        ("target_capacity_kw", 0, "normalization_target_capacity"),
        ("target_capacity_kw", float("inf"), "normalization_target_capacity"),
        ("target_capacity_kw", float("nan"), "normalization_target_capacity"),
    ],
)
def test_normalization_preserves_finite_positive_capacity_provenance(
    field: str, value: float, constraint: str
) -> None:
    system = make_system(approved=True)
    source_job = CollectionJob.objects.get(binding=system.binding)
    source = ForecastSnapshot.objects.create(
        job=source_job,
        binding=system.binding,
        raw_payload={},
        normalized_intervals=[],
        collected_at=NOW,
    )
    job = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="scaled_forecasts",
        scheduled_slot=NOW,
        target_date=NOW.date(),
        available_at=NOW,
    )
    snapshot = ForecastSnapshot.objects.create(
        job=job, binding=system.binding, raw_payload={}, normalized_intervals=[], collected_at=NOW
    )
    values: dict[str, Any] = dict(
        source_snapshot=source,
        snapshot=snapshot,
        target_binding=system.binding,
        source_capacity_kw=1,
        target_capacity_kw=2,
        factor=2,
        authorized_by=system.admin,
        authorization_reference="Authorized fixture conversion",
        created_at=NOW,
    )
    values[field] = value
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        ReferenceCapacityNormalization.objects.create(**values)
