import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from pyracast.plants.models import Plant, PlantEvent
from pyracast.regions.models import Region
from tests.support.domain import NOW
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)


def test_removing_connection_meter_basis_archives_declarations_and_restores_on_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    plants = [plant]
    for number in (1, 2):
        region = Region.objects.create(
            tenant=system.tenant,
            name=f"Additional site {number}",
            latitude=40 + number,
            longitude=29,
            timezone="Europe/Istanbul",
        )
        plants.append(Plant.objects.create(region=region, name=region.name))
    old_target = [("metering", "0006_validate_lineage")]
    new_target = [("metering", "0007_remove_connection_meter_basis")]
    executor = MigrationExecutor(connection)
    current_targets = executor.loader.graph.leaf_nodes()
    bases = ("net_export", "ac_generation", "unknown")
    try:
        executor.migrate(old_target)
        old_connections = executor.loader.project_state(old_target).apps.get_model(
            "metering", "EpiasConnection"
        )
        identifiers = []
        for index, (owner, basis) in enumerate(zip(plants, bases, strict=True), start=1):
            record = old_connections.objects.create(
                plant_id=owner.pk,
                provider_plant_id=index,
                eic=f"EIC-{index}",
                name=owner.name,
                connected_by_id=system.member.pk,
                meter_basis=basis,
                revision=index,
            )
            old_connections.objects.filter(pk=record.pk).update(updated_at=NOW)
            identifiers.append(record.pk)
        MigrationExecutor(connection).migrate(new_target)
        archives = PlantEvent.objects.filter(action="legacy_epias_meter_basis_archived")
        assert archives.count() == 2
        for index, (owner, basis) in enumerate(zip(plants[:2], bases[:2], strict=True), start=1):
            event = archives.get(plant=owner)
            assert event.actor_id is None
            assert event.details == {
                "migration": "metering.0007_remove_connection_meter_basis",
                "connection_id": identifiers[index - 1],
                "connection_revision": index,
                "provider_plant_id": index,
                "eic": f"EIC-{index}",
                "meter_basis": basis,
                "connected_by_id": system.member.pk,
                "connection_updated_at": NOW.isoformat(),
            }
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(
                cursor, "metering_epiasconnection"
            )
        assert "meter_basis" not in {column.name for column in columns}
        MigrationExecutor(connection).migrate(old_target)
        assert list(
            old_connections.objects.filter(pk__in=identifiers)
            .order_by("provider_plant_id")
            .values_list("meter_basis", flat=True)
        ) == list(bases)
        MigrationExecutor(connection).migrate(new_target)
        assert archives.count() == 2
        plant.refresh_from_db()
        assert plant.current_configuration is not None
        assert plant.current_configuration.measurement_basis == "net_export"
    finally:
        MigrationExecutor(connection).migrate(current_targets)
