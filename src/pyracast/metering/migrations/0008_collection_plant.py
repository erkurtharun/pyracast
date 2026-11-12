from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def populate_plants(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT j.id FROM metering_epiascollectionjob j "
            "JOIN metering_epiasconnection e ON e.id = j.connection_id "
            "JOIN plants_plantconfiguration c ON c.id = j.configuration_id "
            "WHERE e.plant_id <> c.plant_id ORDER BY j.id LIMIT 10"
        )
        conflicts = [row[0] for row in cursor.fetchall()]
        if conflicts:
            raise ValueError(
                f"Resolve EPİAŞ job connection/configuration plant mismatches: {conflicts}"
            )
        cursor.execute(
            "UPDATE metering_epiascollectionjob j SET plant_id = e.plant_id "
            "FROM metering_epiasconnection e WHERE e.id = j.connection_id"
        )


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("metering", "0007_remove_connection_meter_basis"),
        ("plants", "0016_validate_configuration_region"),
    ]

    operations: ClassVar = [
        migrations.AddConstraint(
            "epiasconnection",
            models.UniqueConstraint(fields=("id", "plant"), name="epias_connection_plant_identity"),
        ),
        migrations.AddField(
            "epiascollectionjob",
            "plant",
            models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT, to="plants.plant"
            ),
        ),
        migrations.RunPython(populate_plants, migrations.RunPython.noop),
    ]
