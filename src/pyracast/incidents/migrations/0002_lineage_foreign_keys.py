from typing import ClassVar

from django.db import migrations


class Migration(migrations.Migration):
    dependencies: ClassVar = [("incidents", "0001_initial"), ("plants", "0011_database_invariants")]

    operations: ClassVar = [
        migrations.RunSQL(
            (
                "ALTER TABLE incidents_plantincident ADD CONSTRAINT "
                "incident_declared_config_owner_fk FOREIGN KEY (declared_configuration_id, "
                "plant_id) REFERENCES plants_plantconfiguration (id, plant_id) DEFERRABLE "
                "INITIALLY DEFERRED NOT VALID"
            ),
            "ALTER TABLE incidents_plantincident DROP CONSTRAINT incident_declared_config_owner_fk",
        ),
    ]
