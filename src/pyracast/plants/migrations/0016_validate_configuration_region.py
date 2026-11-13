from typing import ClassVar

import django.db.models.deletion
from django.contrib.postgres.operations import ValidateConstraint
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar = [("plants", "0015_configuration_region_constraints")]

    operations: ClassVar = [
        migrations.RunSQL(
            "ALTER TABLE plants_plantconfiguration "
            "VALIDATE CONSTRAINT configuration_plant_region_fk",
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "ALTER TABLE plants_plantconfiguration "
            "VALIDATE CONSTRAINT configuration_binding_region_fk",
            migrations.RunSQL.noop,
        ),
        ValidateConstraint("plantconfiguration", "configuration_region_required"),
        migrations.AlterField(
            "plantconfiguration",
            "region",
            models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="regions.region"),
        ),
        migrations.RemoveConstraint("plantconfiguration", "configuration_region_required"),
    ]
