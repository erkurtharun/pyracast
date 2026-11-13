from typing import ClassVar

from django.db import migrations


class Migration(migrations.Migration):
    dependencies: ClassVar = [("metering", "0004_database_invariants")]

    operations: ClassVar = [
        migrations.RunSQL(
            (
                "ALTER TABLE metering_epiassnapshot ADD CONSTRAINT "
                "epias_snapshot_job_lineage_fk FOREIGN KEY (job_id, connection_id, "
                "configuration_id) REFERENCES metering_epiascollectionjob (id, connection_id, "
                "configuration_id) DEFERRABLE INITIALLY DEFERRED NOT VALID"
            ),
            "ALTER TABLE metering_epiassnapshot DROP CONSTRAINT epias_snapshot_job_lineage_fk",
        ),
    ]
