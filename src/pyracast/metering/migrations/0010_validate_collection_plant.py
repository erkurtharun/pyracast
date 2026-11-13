from typing import ClassVar

import django.db.models.deletion
from django.contrib.postgres.operations import ValidateConstraint
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar = [("metering", "0009_collection_plant_constraints")]

    operations: ClassVar = [
        migrations.RunSQL(
            "ALTER TABLE metering_epiascollectionjob "
            "VALIDATE CONSTRAINT epias_job_connection_plant_fk",
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "ALTER TABLE metering_epiascollectionjob "
            "VALIDATE CONSTRAINT epias_job_configuration_plant_fk",
            migrations.RunSQL.noop,
        ),
        ValidateConstraint("epiascollectionjob", "epias_job_plant_required"),
        migrations.AlterField(
            "epiascollectionjob",
            "plant",
            models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="plants.plant"),
        ),
        migrations.RemoveConstraint("epiascollectionjob", "epias_job_plant_required"),
    ]
