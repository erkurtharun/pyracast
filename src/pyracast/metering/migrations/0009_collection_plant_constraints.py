from typing import ClassVar

from django.contrib.postgres.operations import AddConstraintNotValid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar = [("metering", "0008_collection_plant")]

    operations: ClassVar = [
        migrations.RunSQL(
            "ALTER TABLE metering_epiascollectionjob ADD CONSTRAINT epias_job_connection_plant_fk "
            "FOREIGN KEY (connection_id, plant_id) "
            "REFERENCES metering_epiasconnection (id, plant_id) "
            "DEFERRABLE INITIALLY DEFERRED NOT VALID",
            "ALTER TABLE metering_epiascollectionjob DROP CONSTRAINT epias_job_connection_plant_fk",
        ),
        migrations.RunSQL(
            "ALTER TABLE metering_epiascollectionjob "
            "ADD CONSTRAINT epias_job_configuration_plant_fk "
            "FOREIGN KEY (configuration_id, plant_id) "
            "REFERENCES plants_plantconfiguration (id, plant_id) "
            "DEFERRABLE INITIALLY DEFERRED NOT VALID",
            "ALTER TABLE metering_epiascollectionjob "
            "DROP CONSTRAINT epias_job_configuration_plant_fk",
        ),
        AddConstraintNotValid(
            "epiascollectionjob",
            models.CheckConstraint(
                condition=models.Q(plant__isnull=False), name="epias_job_plant_required"
            ),
        ),
    ]
