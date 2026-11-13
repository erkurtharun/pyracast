from collections.abc import Callable, Iterator
from typing import Any

import pytest
from django.db import OperationalError, connection
from django.db.migrations.executor import MigrationExecutor

from pyracast.metering.models import EpiasCollectionJob, EpiasConnection
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.regions.models import Region
from pyracast.regions.operations import configure_binding
from tests.test_domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)

OLD_TARGETS = [
    ("plants", "0013_validate_lineage"),
    ("metering", "0007_remove_connection_meter_basis"),
]


@pytest.fixture
def legacy_records() -> Iterator[tuple[Any, Any, list[tuple[str, str]]]]:
    system = make_system()
    first = Plant.objects.create(region=system.region, name="First")
    region = Region.objects.create(
        tenant=system.tenant, name="Second", latitude=41, longitude=30, timezone="UTC"
    )
    second = Plant.objects.create(region=region, name="Second")
    binding = configure_binding(
        system.admin,
        region.pk,
        provider_account_id=system.account.pk,
        site_id="second",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    for plant, selected in ((first, system.binding), (second, binding)):
        PlantConfiguration.objects.create(
            plant=plant,
            region_id=plant.region_id,
            binding=selected,
            revision=1,
            dc_capacity_mw=1,
            created_by=system.member,
        )
    configuration = first.configurations.get()
    linked = EpiasConnection.objects.create(
        plant=first, provider_plant_id=1, eic="EIC", name="First", connected_by=system.member
    )
    job = EpiasCollectionJob.objects.create(
        plant=first,
        connection=linked,
        configuration=configuration,
        connection_revision=1,
        provider_plant={"id": 1, "eic": "EIC"},
        target_day=NOW.date(),
        scheduled_for=NOW,
        next_attempt_at=NOW,
    )
    executor = MigrationExecutor(connection)
    current_targets = executor.loader.graph.leaf_nodes()
    executor.migrate(OLD_TARGETS)
    state = executor.loader.project_state(OLD_TARGETS).apps
    old_configuration = state.get_model("plants", "PlantConfiguration").objects.get(
        pk=configuration.pk
    )
    old_job = state.get_model("metering", "EpiasCollectionJob").objects.get(pk=job.pk)
    try:
        yield old_configuration, old_job, current_targets
    finally:
        type(old_configuration).objects.filter(pk=configuration.pk).update(
            binding_id=system.binding.pk
        )
        type(old_job).objects.filter(pk=job.pk).update(configuration_id=configuration.pk)
        MigrationExecutor(connection).migrate(current_targets)


def test_owner_keys_preserve_existing_relationships_and_values(
    legacy_records: tuple[Any, Any, list[tuple[str, str]]],
) -> None:
    old_configuration, old_job, current_targets = legacy_records
    MigrationExecutor(connection).migrate(current_targets)
    configuration = PlantConfiguration.objects.get(pk=old_configuration.pk)
    job = EpiasCollectionJob.objects.get(pk=old_job.pk)
    assert (
        configuration.region_id == configuration.plant.region_id == configuration.binding.region_id
    )
    assert configuration.binding_id == old_configuration.binding_id
    assert configuration.dc_capacity_mw == old_configuration.dc_capacity_mw
    assert job.plant_id == job.connection.plant_id == job.configuration.plant_id
    assert (job.connection_id, job.configuration_id, job.provider_plant) == (
        old_job.connection_id,
        old_job.configuration_id,
        old_job.provider_plant,
    )


@pytest.mark.parametrize("record", ["configuration", "job"])
def test_owner_key_migration_refuses_legacy_mismatch_without_rewriting_relationships(
    legacy_records: tuple[Any, Any, list[tuple[str, str]]], record: str
) -> None:
    old_configuration, old_job, current_targets = legacy_records
    other = type(old_configuration).objects.exclude(pk=old_configuration.pk).get()
    if record == "configuration":
        type(old_configuration).objects.filter(pk=old_configuration.pk).update(
            binding_id=other.binding_id
        )
    else:
        type(old_job).objects.filter(pk=old_job.pk).update(configuration_id=other.pk)
    with pytest.raises(ValueError, match="mismatches"):
        MigrationExecutor(connection).migrate(current_targets)
    old_configuration.refresh_from_db()
    old_job.refresh_from_db()
    if record == "configuration":
        assert old_configuration.binding_id == other.binding_id
    else:
        assert old_job.configuration_id == other.pk


@pytest.mark.parametrize(
    ("app", "previous", "target", "table", "column"),
    [
        (
            "plants",
            "0015_configuration_region_constraints",
            "0016_validate_configuration_region",
            "plants_plantconfiguration",
            "region_id",
        ),
        (
            "metering",
            "0009_collection_plant_constraints",
            "0010_validate_collection_plant",
            "metering_epiascollectionjob",
            "plant_id",
        ),
    ],
)
def test_interrupted_owner_key_finalization_preserves_foreign_key(
    app: str, previous: str, target: str, table: str, column: str
) -> None:
    executor = MigrationExecutor(connection)
    current_targets = executor.loader.graph.leaf_nodes()
    executor.migrate([(app, previous)])
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
            "WHERE c.conrelid = %s::regclass AND c.contype = 'f' "
            "AND array_length(c.conkey, 1) = 1 AND a.attname = %s",
            [table, column],
        )
        constraint, definition = cursor.fetchone()
    quote = connection.ops.quote_name
    interrupted_statement = f"ALTER TABLE {quote(table)} ALTER COLUMN {quote(column)} SET NOT NULL"

    def interrupt_finalization(
        execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any
    ) -> Any:
        if interrupted_statement in sql:
            raise OperationalError("Injected migration interruption")
        return execute(sql, params, many, context)

    try:
        with (
            connection.execute_wrapper(interrupt_finalization),
            pytest.raises(OperationalError, match="Injected migration interruption"),
        ):
            MigrationExecutor(connection).migrate([(app, target)])
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_constraint WHERE conrelid = %s::regclass AND conname = %s",
                [table, constraint],
            )
            assert cursor.fetchone()[0] == 1
            cursor.execute(
                "SELECT attnotnull FROM pg_attribute "
                "WHERE attrelid = %s::regclass AND attname = %s",
                [table, column],
            )
            assert cursor.fetchone() == (False,)
        assert (app, target) not in MigrationExecutor(connection).loader.applied_migrations
        MigrationExecutor(connection).migrate([(app, target)])
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_constraint WHERE conrelid = %s::regclass AND conname = %s",
                [table, constraint],
            )
            if cursor.fetchone()[0] == 0:
                cursor.execute(
                    f"ALTER TABLE {quote(table)} ADD CONSTRAINT {quote(constraint)} {definition}"
                )
        MigrationExecutor(connection).migrate(current_targets)
