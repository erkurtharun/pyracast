from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast.db import refuse_values_outside

LAST_ERRORS = [
    "",
    "missed_schedule",
    "delivery_deadline_exceeded",
    "cancelled_by_settings_change",
    "schedule_or_authority_changed",
    "no_recipients",
    "invalid_recipient",
    "recipient_limit_exceeded",
    "definition_trashed",
    "interrupted_send",
    "interrupted_connection",
    "mail_not_sent",
    "mail_configuration_invalid",
    "mail_credentials_rejected",
    "mail_authorization_revoked",
    "mail_rate_limited",
    "mail_rejected",
    "mail_sender_forbidden",
    "mail_preparation_failed",
    "mail_outcome_uncertain",
]

FORMATS = ["xlsx", "plain"]

KINDS = ["current", "saved_complete", "saved_partial", "unavailable"]


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0002_initial"),
        ("forecasts", "0002_collection_endpoint_and_request_purpose_checks"),
        ("notifications", "0001_initial"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AlterField(
            model_name="notificationdelivery",
            name="last_error",
            field=models.CharField(
                blank=True,
                choices=[
                    ("missed_schedule", "Missed schedule"),
                    ("delivery_deadline_exceeded", "Delivery deadline exceeded"),
                    ("cancelled_by_settings_change", "Cancelled by settings change"),
                    ("schedule_or_authority_changed", "Schedule or authority changed"),
                    ("no_recipients", "No recipients"),
                    ("invalid_recipient", "Invalid recipient"),
                    ("recipient_limit_exceeded", "Recipient limit exceeded"),
                    ("definition_trashed", "Definition trashed"),
                    ("interrupted_send", "Interrupted during sending"),
                    ("interrupted_connection", "Interrupted before sending"),
                    ("mail_not_sent", "Mail not sent"),
                    ("mail_configuration_invalid", "Mail configuration invalid"),
                    ("mail_credentials_rejected", "Mail credentials rejected"),
                    ("mail_authorization_revoked", "Mail authorization revoked"),
                    ("mail_rate_limited", "Mail rate limited"),
                    ("mail_rejected", "Mail rejected"),
                    ("mail_sender_forbidden", "Mail sender forbidden"),
                    ("mail_preparation_failed", "Mail preparation failed"),
                    ("mail_outcome_uncertain", "Mail outcome uncertain"),
                ],
                default="",
                max_length=40,
            ),
        ),
        migrations.AlterField(
            model_name="notificationdelivery",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("retry", "Retry"),
                    ("sending", "Sending"),
                    ("sent", "Sent"),
                    ("failed", "Failed"),
                    ("uncertain", "Uncertain"),
                    ("skipped", "Skipped"),
                    ("cancelled", "Cancelled"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        refuse_values_outside("notifications.notificationdelivery", "last_error", LAST_ERRORS),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(("last_error__in", LAST_ERRORS)),
                name="notificationdelivery_last_error_chk",
            ),
        ),
        refuse_values_outside(
            "notifications.notificationdelivery",
            "prepared_message__format",
            FORMATS,
            scope=models.Q(prepared_message__has_key="format"),
        ),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(("prepared_message__format__in", FORMATS)),
                name="notificationdelivery_prepared_format_chk",
            ),
        ),
        refuse_values_outside(
            "notifications.notificationdelivery",
            "prepared_message__kind",
            KINDS,
            scope=models.Q(prepared_message__has_key="kind"),
        ),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(("prepared_message__kind__in", KINDS)),
                name="notificationdelivery_prepared_kind_chk",
            ),
        ),
    ]
