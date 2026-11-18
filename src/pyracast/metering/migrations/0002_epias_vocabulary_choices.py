from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("metering", "0001_initial"),
    ]

    operations: ClassVar[list[Operation]] = [
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
    ]
