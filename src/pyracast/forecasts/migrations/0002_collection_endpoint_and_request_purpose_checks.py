from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast.db import refuse_values_outside

ENDPOINTS = [
    "forecasts",
    "estimated_actuals",
    "historical_import",
    "scaled_forecasts",
    "scaled_estimated_actuals",
]

PURPOSES = ["forecast", "estimated_actuals", "validation", "inspect_settings", "inspect_forecast"]


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0002_initial"),
        ("forecasts", "0001_initial"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AlterField(
            model_name="collectionjob",
            name="endpoint",
            field=models.CharField(
                choices=[
                    ("forecasts", "Forecasts"),
                    ("estimated_actuals", "Estimated actuals"),
                    ("historical_import", "Historical import"),
                    ("scaled_forecasts", "Scaled forecasts"),
                    ("scaled_estimated_actuals", "Scaled estimated actuals"),
                ],
                default="forecasts",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="collectionjob",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("running", "Running"),
                    ("retry", "Retry"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("stale", "Stale"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="forecastsnapshot",
            name="source_kind",
            field=models.CharField(
                choices=[("forecast", "Forecast"), ("estimated_actuals", "Estimated actuals")],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="requestattempt",
            name="outcome",
            field=models.CharField(
                choices=[
                    ("uncertain", "Uncertain"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                ],
                default="uncertain",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="requestattempt",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("forecast", "Forecast"),
                    ("estimated_actuals", "Estimated actuals"),
                    ("validation", "Validation"),
                    ("inspect_settings", "Site settings check"),
                    ("inspect_forecast", "Forecast check"),
                ],
                max_length=20,
            ),
        ),
        refuse_values_outside("forecasts.collectionjob", "endpoint", ENDPOINTS),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(("endpoint__in", ENDPOINTS)),
                name="collectionjob_endpoint_chk",
            ),
        ),
        refuse_values_outside("forecasts.requestattempt", "purpose", PURPOSES),
        migrations.AddConstraint(
            model_name="requestattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(("purpose__in", PURPOSES)),
                name="requestattempt_purpose_chk",
            ),
        ),
    ]
