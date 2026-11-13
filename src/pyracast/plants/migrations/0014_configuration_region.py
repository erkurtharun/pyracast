from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def populate_regions(_apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.id FROM plants_plantconfiguration c "
            "JOIN plants_plant p ON p.id = c.plant_id "
            "JOIN connections_sitebinding b ON b.id = c.binding_id "
            "WHERE p.region_id <> b.region_id ORDER BY c.id LIMIT 10"
        )
        conflicts = [row[0] for row in cursor.fetchall()]
        if conflicts:
            raise ValueError(f"Resolve configuration plant/binding region mismatches: {conflicts}")
        cursor.execute(
            "UPDATE plants_plantconfiguration c SET region_id = p.region_id "
            "FROM plants_plant p WHERE p.id = c.plant_id"
        )


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("plants", "0013_validate_lineage"),
        ("connections", "0009_validate_binding_integrity"),
    ]

    operations: ClassVar = [
        migrations.AddConstraint(
            "plant", models.UniqueConstraint(fields=("id", "region"), name="plant_region_identity")
        ),
        migrations.AddField(
            "plantconfiguration",
            "region",
            models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT, to="regions.region"
            ),
        ),
        migrations.RunPython(populate_regions, migrations.RunPython.noop),
    ]
