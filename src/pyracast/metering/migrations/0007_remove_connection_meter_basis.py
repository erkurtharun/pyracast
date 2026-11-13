from typing import ClassVar

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

ARCHIVE_ACTION = "legacy_epias_meter_basis_archived"
MIGRATION_NAME = "metering.0007_remove_connection_meter_basis"


def archive_declarations(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    connection_model = apps.get_model("metering", "EpiasConnection")
    event_model = apps.get_model("plants", "PlantEvent")
    alias = schema_editor.connection.alias
    for connection in (
        connection_model.objects.using(alias).exclude(meter_basis="unknown").iterator()
    ):
        event_model.objects.using(alias).get_or_create(
            plant_id=connection.plant_id,
            actor_id=None,
            action=ARCHIVE_ACTION,
            details={
                "migration": MIGRATION_NAME,
                "connection_id": connection.pk,
                "connection_revision": connection.revision,
                "provider_plant_id": connection.provider_plant_id,
                "eic": connection.eic,
                "meter_basis": connection.meter_basis,
                "connected_by_id": connection.connected_by_id,
                "connection_updated_at": connection.updated_at.isoformat(),
            },
        )


def restore_declarations(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    connection_model = apps.get_model("metering", "EpiasConnection")
    event_model = apps.get_model("plants", "PlantEvent")
    alias = schema_editor.connection.alias
    events = event_model.objects.using(alias).filter(
        action=ARCHIVE_ACTION, details__migration=MIGRATION_NAME
    )
    for event in events.order_by("pk").iterator():
        details = event.details
        connection_model.objects.using(alias).filter(
            pk=details["connection_id"],
            plant_id=event.plant_id,
            revision=details["connection_revision"],
            provider_plant_id=details["provider_plant_id"],
            eic=details["eic"],
            meter_basis="unknown",
        ).update(meter_basis=details["meter_basis"])


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("metering", "0006_validate_lineage"),
        ("plants", "0013_validate_lineage"),
    ]

    operations: ClassVar = [
        migrations.RunPython(archive_declarations, restore_declarations),
        migrations.RemoveConstraint("epiasconnection", "epias_meter_basis_valid"),
        migrations.RemoveField("epiasconnection", "meter_basis"),
    ]
