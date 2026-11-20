from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0004_schema_naming_convention"),
        ("forecasts", "0005_collection_times_shape_check"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RemoveConstraint(
            model_name="collectionjob",
            name="collectionjob_last_error_chk",
        ),
        migrations.AddField(
            model_name="requestattempt",
            name="http_status",
            field=models.PositiveSmallIntegerField(null=True),
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
                    ("binding_not_current", "Binding not current"),
                    ("schedule_disabled", "Schedule disabled"),
                    ("slot_not_configured", "Slot not configured"),
                    ("schedule_changed", "Schedule changed"),
                    ("history_incomplete", "History incomplete"),
                    ("history_already_archived", "History already archived"),
                    ("provider_authentication_failed", "Provider authentication failed"),
                    ("provider_allowance_exhausted", "Provider allowance exhausted"),
                    ("provider_access_denied", "Provider access denied"),
                    ("provider_site_not_found", "Provider site not found"),
                    ("provider_throttled", "Provider throttled"),
                    ("provider_server_error", "Provider server error"),
                    ("provider_unreachable", "Provider unreachable"),
                    ("provider_invalid_response", "Provider invalid response"),
                    ("provider_configuration_invalid", "Provider configuration invalid"),
                    ("account_unavailable", "Account unavailable"),
                    ("history_budget_reserved", "History budget reserved"),
                    ("quota_exhausted", "Quota exhausted"),
                    ("provider_failure", "Provider failure"),
                    ("provider_unexpected_failure", "Provider unexpected failure"),
                    ("invalid_source_kind", "Invalid source kind"),
                    ("calculation_failure", "Calculation failure"),
                    ("lock_contention", "Lock contention"),
                    ("publication_failure", "Publication failure"),
                ],
                default="",
                max_length=40,
            ),
        ),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "last_error__in",
                        [
                            "",
                            "history_window_closed",
                            "attempts_exhausted",
                            "forecast_superseded",
                            "binding_not_current",
                            "schedule_disabled",
                            "slot_not_configured",
                            "schedule_changed",
                            "history_incomplete",
                            "history_already_archived",
                            "provider_authentication_failed",
                            "provider_allowance_exhausted",
                            "provider_access_denied",
                            "provider_site_not_found",
                            "provider_throttled",
                            "provider_server_error",
                            "provider_unreachable",
                            "provider_invalid_response",
                            "provider_configuration_invalid",
                            "account_unavailable",
                            "history_budget_reserved",
                            "quota_exhausted",
                            "provider_failure",
                            "provider_unexpected_failure",
                            "invalid_source_kind",
                            "calculation_failure",
                            "lock_contention",
                            "publication_failure",
                        ],
                    )
                ),
                name="collectionjob_last_error_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="requestattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("http_status__isnull", True),
                    models.Q(("http_status__gte", 100), ("http_status__lte", 599)),
                    _connector="OR",
                ),
                name="requestattempt_http_status_chk",
            ),
        ),
    ]
