from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast.db import refuse_values_outside

JOB_ERRORS = [
    "",
    "retry_window_expired",
    "attempts_exhausted",
    "epias_provider_failure",
    "unpublished_data",
    "partial_data",
]


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("metering", "0001_initial"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AlterField(
            model_name="epiascollectionjob",
            name="last_error",
            field=models.CharField(
                blank=True,
                choices=[
                    ("retry_window_expired", "Retry window expired"),
                    ("attempts_exhausted", "Attempts exhausted"),
                    ("epias_provider_failure", "Epias provider failure"),
                    ("unpublished_data", "Unpublished data"),
                    ("partial_data", "Partial data"),
                ],
                default="",
                max_length=40,
            ),
        ),
        migrations.AlterField(
            model_name="epiascollectionjob",
            name="source_kind",
            field=models.CharField(
                choices=[
                    ("realtime_generation", "Real-time generation"),
                    ("settlement_injection", "Settlement injection"),
                ],
                default="realtime_generation",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="epiascollectionjob",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("running", "Running"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("superseded", "Superseded"),
                    ("awaiting_publication", "Awaiting publication"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="epiassnapshot",
            name="source_kind",
            field=models.CharField(
                choices=[
                    ("realtime_generation", "Real-time generation"),
                    ("settlement_injection", "Settlement injection"),
                ],
                default="realtime_generation",
                max_length=30,
            ),
        ),
        refuse_values_outside("metering.epiascollectionjob", "last_error", JOB_ERRORS),
        migrations.AddConstraint(
            model_name="epiascollectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(("last_error__in", JOB_ERRORS)),
                name="epiascollectionjob_last_error_chk",
            ),
        ),
    ]
