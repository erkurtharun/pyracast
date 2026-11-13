from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0006_reference_dc_capacity"),
        ("forecasts", "0006_snapshot_weather_reference"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="HistoricalReferenceImport",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("sha256", models.CharField(max_length=64)),
                ("source_name", models.CharField(max_length=255)),
                ("source_settings", models.JSONField()),
                ("imported_at", models.DateTimeField()),
                ("interval_count", models.PositiveIntegerField()),
            ],
        ),
        migrations.RemoveConstraint(
            model_name="collectionjob",
            name="unique_collection_slot",
        ),
        migrations.AddField(
            model_name="historicalreferenceimport",
            name="binding",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="connections.sitebinding"
            ),
        ),
        migrations.AddField(
            model_name="historicalreferenceimport",
            name="imported_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name="collectionjob",
            name="reference_import",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="forecasts.historicalreferenceimport",
            ),
        ),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.UniqueConstraint(
                condition=models.Q(("reference_import__isnull", True)),
                fields=("binding", "endpoint", "scheduled_slot"),
                name="unique_collection_slot",
            ),
        ),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.UniqueConstraint(
                fields=("reference_import", "target_date"), name="unique_import_reference_day"
            ),
        ),
        migrations.AddConstraint(
            model_name="historicalreferenceimport",
            constraint=models.UniqueConstraint(
                fields=("binding", "sha256"), name="unique_historical_reference_import"
            ),
        ),
    ]
