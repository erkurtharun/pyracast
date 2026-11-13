from datetime import datetime, timedelta
from typing import ClassVar

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def normalize_archived_readings(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    snapshots = apps.get_model("metering", "EpiasSnapshot")
    for snapshot in (
        snapshots.objects.using(schema_editor.connection.alias).select_related("job").iterator()
    ):
        readings = []
        for original in snapshot.readings:
            reading = dict(original)
            start = datetime.fromisoformat(reading["provider_timestamp"])
            reading["period_start"] = start.isoformat()
            reading["period_end"] = (start + timedelta(hours=1)).isoformat()
            readings.append(reading)
        snapshots.objects.using(schema_editor.connection.alias).filter(pk=snapshot.pk).update(
            readings=readings,
            provider_plant=snapshot.job.provider_plant,
            endpoint=(
                "https://seffaflik.epias.com.tr/electricity-service/v1/generation/data/"
                "realtime-generation"
            ),
        )


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("metering", "0001_initial"),
        ("plants", "0005_plantconfiguration_mounting_geometry"),
    ]

    operations: ClassVar = [
        migrations.RemoveConstraint(
            model_name="epiascollectionjob",
            name="epias_unique_collection_slot",
        ),
        migrations.RemoveConstraint(
            model_name="epiassnapshot",
            name="epias_unique_job_acquisition",
        ),
        migrations.AddField(
            model_name="epiascollectionjob",
            name="period_end_day",
            field=models.DateField(null=True),
        ),
        migrations.AddField(
            model_name="epiascollectionjob",
            name="source_kind",
            field=models.CharField(default="realtime_generation", max_length=30),
        ),
        migrations.AddField(
            model_name="epiasconnection",
            name="settlement_plant_id",
            field=models.PositiveBigIntegerField(null=True),
        ),
        migrations.AddField(
            model_name="epiasconnection",
            name="settlement_checked_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="epiassnapshot",
            name="endpoint",
            field=models.URLField(default=""),
        ),
        migrations.AddField(
            model_name="epiassnapshot",
            name="normalization_policy",
            field=models.CharField(default="hour-start-istanbul-v1", max_length=40),
        ),
        migrations.AddField(
            model_name="epiassnapshot",
            name="provider_plant",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="epiassnapshot",
            name="source_kind",
            field=models.CharField(default="realtime_generation", max_length=30),
        ),
        migrations.RunPython(normalize_archived_readings, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="epiascollectionjob",
            constraint=models.UniqueConstraint(
                fields=(
                    "connection",
                    "connection_revision",
                    "configuration",
                    "source_kind",
                    "target_day",
                    "scheduled_for",
                ),
                name="epias_unique_collection_slot",
            ),
        ),
        migrations.AddConstraint(
            model_name="epiassnapshot",
            constraint=models.UniqueConstraint(
                fields=("job", "attempt", "target_day"), name="epias_unique_job_acquisition"
            ),
        ),
    ]
