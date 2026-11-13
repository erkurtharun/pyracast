from typing import ClassVar

from django.contrib.postgres.operations import AddConstraintNotValid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar = [("plants", "0014_configuration_region")]

    operations: ClassVar = [
        migrations.RunSQL(
            "ALTER TABLE plants_plantconfiguration "
            "ADD CONSTRAINT configuration_plant_region_fk "
            "FOREIGN KEY (plant_id, region_id) REFERENCES plants_plant (id, region_id) "
            "DEFERRABLE INITIALLY DEFERRED NOT VALID",
            "ALTER TABLE plants_plantconfiguration DROP CONSTRAINT configuration_plant_region_fk",
        ),
        migrations.RunSQL(
            "ALTER TABLE plants_plantconfiguration "
            "ADD CONSTRAINT configuration_binding_region_fk "
            "FOREIGN KEY (binding_id, region_id) "
            "REFERENCES connections_sitebinding (id, region_id) "
            "DEFERRABLE INITIALLY DEFERRED NOT VALID",
            "ALTER TABLE plants_plantconfiguration DROP CONSTRAINT configuration_binding_region_fk",
        ),
        AddConstraintNotValid(
            "plantconfiguration",
            models.CheckConstraint(
                condition=models.Q(region__isnull=False), name="configuration_region_required"
            ),
        ),
    ]
