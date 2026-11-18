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

JOB_ERRORS = [
    "",
    "history_window_closed",
    "attempts_exhausted",
    "forecast_superseded",
    "history_incomplete",
    "history_already_archived",
    "provider_authentication_failed",
    "provider_throttled",
    "history_budget_reserved",
    "quota_exhausted",
    "provider_failure",
    "provider_unexpected_failure",
    "invalid_source_kind",
    "calculation_failure",
]

WEATHER_ERRORS = [
    "",
    "invalid_weather_number",
    "invalid_weather_coordinates",
    "invalid_weather_schema",
    "invalid_weather_timezone",
    "invalid_weather_units",
    "empty_weather_response",
    "invalid_weather_interval",
    "invalid_weather_window",
    "unsafe_weather_response",
    "invalid_weather_collection_time",
    "weather_request_failed",
    "weather_acquisition_failed",
    "weather_acquisition_interrupted",
]


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
        migrations.AlterField(
            model_name="collectionjob",
            name="last_error",
            field=models.CharField(
                blank=True,
                choices=[
                    ("history_window_closed", "History window closed"),
                    ("attempts_exhausted", "Attempts exhausted"),
                    ("forecast_superseded", "Forecast superseded"),
                    ("history_incomplete", "History incomplete"),
                    ("history_already_archived", "History already archived"),
                    ("provider_authentication_failed", "Provider authentication failed"),
                    ("provider_throttled", "Provider throttled"),
                    ("history_budget_reserved", "History budget reserved"),
                    ("quota_exhausted", "Quota exhausted"),
                    ("provider_failure", "Provider failure"),
                    ("provider_unexpected_failure", "Provider unexpected failure"),
                    ("invalid_source_kind", "Invalid source kind"),
                    ("calculation_failure", "Calculation failure"),
                ],
                default="",
                max_length=40,
            ),
        ),
        migrations.AlterField(
            model_name="forecastsnapshot",
            name="weather_error",
            field=models.CharField(
                blank=True,
                choices=[
                    ("invalid_weather_number", "Invalid weather number"),
                    ("invalid_weather_coordinates", "Invalid weather coordinates"),
                    ("invalid_weather_schema", "Invalid weather schema"),
                    ("invalid_weather_timezone", "Invalid weather timezone"),
                    ("invalid_weather_units", "Invalid weather units"),
                    ("empty_weather_response", "Empty weather response"),
                    ("invalid_weather_interval", "Invalid weather interval"),
                    ("invalid_weather_window", "Invalid weather window"),
                    ("unsafe_weather_response", "Unsafe weather response"),
                    ("invalid_weather_collection_time", "Invalid weather collection time"),
                    ("weather_request_failed", "Weather request failed"),
                    ("weather_acquisition_failed", "Weather acquisition failed"),
                    ("weather_acquisition_interrupted", "Weather acquisition interrupted"),
                ],
                default="",
                max_length=64,
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
        refuse_values_outside("forecasts.collectionjob", "last_error", JOB_ERRORS),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(("last_error__in", JOB_ERRORS)),
                name="collectionjob_last_error_chk",
            ),
        ),
        refuse_values_outside("forecasts.forecastsnapshot", "weather_error", WEATHER_ERRORS),
        migrations.AddConstraint(
            model_name="forecastsnapshot",
            constraint=models.CheckConstraint(
                condition=models.Q(("weather_error__in", WEATHER_ERRORS)),
                name="forecastsnapshot_weather_error_chk",
            ),
        ),
    ]
