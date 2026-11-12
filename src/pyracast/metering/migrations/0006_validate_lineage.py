from typing import ClassVar

from django.contrib.postgres.operations import ValidateConstraint
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False
    dependencies: ClassVar = [("metering", "0005_lineage_foreign_keys")]

    operations: ClassVar = [
        ValidateConstraint("epiascollectionjob", "epias_job_period_valid"),
        ValidateConstraint("epiascollectionjob", "epias_job_revision_positive"),
        ValidateConstraint("epiascollectionjob", "epias_job_source_valid"),
        ValidateConstraint("epiascollectionjob", "epias_job_status_valid"),
        ValidateConstraint("epiasconnection", "epias_meter_basis_valid"),
        ValidateConstraint("epiasconnection", "epias_revision_positive"),
        ValidateConstraint("epiassnapshot", "epias_acquisition_attempt_positive"),
        ValidateConstraint("epiassnapshot", "epias_acquisition_source_valid"),
        migrations.RunSQL(
            "ALTER TABLE metering_epiassnapshot VALIDATE CONSTRAINT epias_snapshot_job_lineage_fk",
            migrations.RunSQL.noop,
        ),
    ]
