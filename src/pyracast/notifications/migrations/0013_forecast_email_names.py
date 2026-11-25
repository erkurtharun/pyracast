from typing import ClassVar

import django.core.validators
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [("notifications", "0012_recipient_limit_ceiling")]

    operations: ClassVar[list[Operation]] = [
        migrations.AlterModelOptions(
            name="notificationsettings",
            options={
                "verbose_name": "forecast email settings",
                "verbose_name_plural": "forecast email settings",
            },
        ),
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
                    ("definition_trashed", "Plant in recycle bin"),
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
                    ("pending", "Preparing"),
                    ("retry", "Will retry"),
                    ("sending", "Sending"),
                    ("sent", "Sent"),
                    ("failed", "Not sent"),
                    ("uncertain", "Not known whether sent"),
                    ("skipped", "Skipped"),
                    ("cancelled", "Cancelled"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="notificationsettings",
            name="max_recipients",
            field=models.PositiveSmallIntegerField(
                default=5,
                help_text=(
                    "Recipient limit of each plant's forecast email. Lowering it pauses the "
                    "forecast email of plants over the limit until their recipient lists are "
                    "shortened."
                ),
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(100),
                ],
            ),
        ),
    ]
