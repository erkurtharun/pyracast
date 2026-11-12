from typing import ClassVar

from django.db import migrations


class Migration(migrations.Migration):
    atomic = False
    dependencies: ClassVar = [("incidents", "0002_lineage_foreign_keys")]

    operations: ClassVar = [
        migrations.RunSQL(
            (
                "ALTER TABLE incidents_plantincident VALIDATE CONSTRAINT "
                "incident_declared_config_owner_fk"
            ),
            migrations.RunSQL.noop,
        ),
    ]
