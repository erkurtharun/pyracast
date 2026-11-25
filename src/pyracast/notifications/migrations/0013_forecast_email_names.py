from typing import ClassVar

import django.core.validators
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

MAIL_KINDS = [
    ("workspace_invitation", "Workspace invitation"),
    ("account/email/email_confirmation_signup", "Sign-up code"),
    ("account/email/email_confirmation", "Verification code"),
    ("account/email/password_reset_code", "Password reset code"),
    ("account/email/login_code", "Confirmation code"),
    ("account/email/password_changed", "Password changed"),
    ("account/email/password_set", "Password set"),
    ("account/email/password_reset", "Password reset"),
    ("account/email/email_changed", "Email address changed"),
    ("account/email/email_confirm", "Email address verified"),
    ("account/email/email_deleted", "Email address removed"),
    ("mfa/email/totp_activated", "Two-factor authentication on"),
    ("mfa/email/totp_deactivated", "Two-factor authentication off"),
    ("account/email/second_factor_reset", "Second factor reset"),
    ("mfa/email/recovery_codes_generated", "Recovery codes generated"),
    ("socialaccount/email/account_connected", "Sign-in provider connected"),
    ("socialaccount/email/account_disconnected", "Sign-in provider disconnected"),
    ("account/email/unknown_account", "Account not found"),
    ("account/email/account_already_exists", "Account already exists"),
    ("account/email/login_locked", "Password sign-in paused"),
    ("plant_approved", "Plant approved"),
    ("plant_rejected", "Plant not approved"),
    ("plant_suspended", "Plant suspended"),
    ("workspace_admin_assigned", "Workspace administrator role received"),
    ("workspace_admin_released", "Workspace administrator role moved away"),
    ("plant_purge_warning", "Plant deletion warning"),
]


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
                    "Lowering the limit pauses the forecast email of plants whose saved "
                    "recipient list is over it. The lists are kept. Once a list is shortened or "
                    "the limit is raised, the next forecast emails are sent at their scheduled "
                    "times."
                ),
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(100),
                ],
                verbose_name="Recipients per plant",
            ),
        ),
        migrations.AlterField(
            model_name="outboundmail",
            name="kind",
            field=models.CharField(choices=MAIL_KINDS, max_length=48),
        ),
        migrations.AlterField(
            model_name="outboundmail",
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
                ],
                default="pending",
                max_length=9,
            ),
        ),
    ]
