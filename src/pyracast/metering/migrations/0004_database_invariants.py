from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.contrib.postgres.operations import AddConstraintNotValid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("metering", "0003_epiascollectionjob_retry_until"),
        ("plants", "0010_plantsubmission_plant_one_active_plant_per_region_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.AlterField(
            model_name="epiascollectionjob",
            name="configuration",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="plants.plantconfiguration"
            ),
        ),
        migrations.AlterField(
            model_name="epiascollectionjob",
            name="connection",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="metering.epiasconnection"
            ),
        ),
        migrations.AlterField(
            model_name="epiasconnection",
            name="plant",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT, to="plants.plant"
            ),
        ),
        migrations.AlterField(
            model_name="epiassnapshot",
            name="configuration",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="plants.plantconfiguration"
            ),
        ),
        migrations.AlterField(
            model_name="epiassnapshot",
            name="connection",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="metering.epiasconnection"
            ),
        ),
        migrations.AlterField(
            model_name="epiassnapshot",
            name="job",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="metering.epiascollectionjob"
            ),
        ),
        migrations.AddConstraint(
            model_name="epiascollectionjob",
            constraint=models.UniqueConstraint(
                fields=("id", "connection", "configuration"), name="epias_job_lineage_unique"
            ),
        ),
        AddConstraintNotValid(
            model_name="epiascollectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("period_end_day__isnull", True),
                    ("period_end_day__gte", models.F("target_day")),
                    _connector="OR",
                ),
                name="epias_job_period_valid",
            ),
        ),
        AddConstraintNotValid(
            model_name="epiascollectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(("connection_revision__gte", 1)),
                name="epias_job_revision_positive",
            ),
        ),
        AddConstraintNotValid(
            model_name="epiascollectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("source_kind__in", ("realtime_generation", "settlement_injection"))
                ),
                name="epias_job_source_valid",
            ),
        ),
        AddConstraintNotValid(
            model_name="epiascollectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "status__in",
                        (
                            "pending",
                            "running",
                            "succeeded",
                            "failed",
                            "superseded",
                            "awaiting_publication",
                        ),
                    )
                ),
                name="epias_job_status_valid",
            ),
        ),
        AddConstraintNotValid(
            model_name="epiasconnection",
            constraint=models.CheckConstraint(
                condition=models.Q(("meter_basis__in", ("unknown", "net_export", "ac_generation"))),
                name="epias_meter_basis_valid",
            ),
        ),
        AddConstraintNotValid(
            model_name="epiasconnection",
            constraint=models.CheckConstraint(
                condition=models.Q(("revision__gte", 1)), name="epias_revision_positive"
            ),
        ),
        AddConstraintNotValid(
            model_name="epiassnapshot",
            constraint=models.CheckConstraint(
                condition=models.Q(("attempt__gte", 1)), name="epias_acquisition_attempt_positive"
            ),
        ),
        AddConstraintNotValid(
            model_name="epiassnapshot",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("source_kind__in", ("realtime_generation", "settlement_injection"))
                ),
                name="epias_acquisition_source_valid",
            ),
        ),
    ]
