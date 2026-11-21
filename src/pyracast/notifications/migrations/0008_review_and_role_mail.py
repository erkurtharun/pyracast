from typing import ClassVar

import django.db.models.lookups
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import pyracast.db


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("notifications", "0007_reminder_recipient_rows"),
    ]

    # Adds mail kinds, their context key and outcomes; every stored row keeps its values.
    operations: ClassVar[list[Operation]] = [
        migrations.RemoveConstraint(
            model_name="outboundmail",
            name="outboundmail_kind_chk",
        ),
        migrations.RemoveConstraint(
            model_name="outboundmail",
            name="outboundmail_last_error_chk",
        ),
        migrations.RemoveConstraint(
            model_name="outboundmail",
            name="outboundmail_context_chk",
        ),
        migrations.AlterField(
            model_name="outboundmail",
            name="kind",
            field=models.CharField(
                choices=[
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
                    ("plant_rejected", "Plant rejected"),
                    ("plant_suspended", "Plant suspended"),
                    ("workspace_admin_assigned", "Administrator role received"),
                    ("workspace_admin_released", "Administrator role moved away"),
                ],
                max_length=48,
            ),
        ),
        migrations.AlterField(
            model_name="outboundmail",
            name="last_error",
            field=models.CharField(
                blank=True,
                choices=[
                    ("expired", "Expired before sending"),
                    ("budget_exhausted", "Hourly budget exhausted"),
                    ("invitation_unavailable", "Invitation closed"),
                    ("no_account", "No account"),
                    ("subject_unavailable", "Reported item no longer available"),
                    ("recipient_unavailable", "Recipient no longer a member"),
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
                max_length=30,
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmail",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "kind__in",
                        [
                            "workspace_invitation",
                            "account/email/email_confirmation_signup",
                            "account/email/email_confirmation",
                            "account/email/password_reset_code",
                            "account/email/login_code",
                            "account/email/password_changed",
                            "account/email/password_set",
                            "account/email/password_reset",
                            "account/email/email_changed",
                            "account/email/email_confirm",
                            "account/email/email_deleted",
                            "mfa/email/totp_activated",
                            "mfa/email/totp_deactivated",
                            "account/email/second_factor_reset",
                            "mfa/email/recovery_codes_generated",
                            "socialaccount/email/account_connected",
                            "socialaccount/email/account_disconnected",
                            "account/email/unknown_account",
                            "account/email/account_already_exists",
                            "account/email/login_locked",
                            "plant_approved",
                            "plant_rejected",
                            "plant_suspended",
                            "workspace_admin_assigned",
                            "workspace_admin_released",
                        ],
                    )
                ),
                name="outboundmail_kind_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmail",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "last_error__in",
                        [
                            "",
                            "expired",
                            "budget_exhausted",
                            "invitation_unavailable",
                            "no_account",
                            "subject_unavailable",
                            "recipient_unavailable",
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
                        ],
                    )
                ),
                name="outboundmail_last_error_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmail",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    django.db.models.lookups.Exact(pyracast.db.JsonType("context"), "object"),
                    models.Q(
                        pyracast.db.JsonPathExists(
                            "context",
                            models.Value(
                                'strict $.keyvalue() ? (@.value.type() != "string"'
                                ' || !(@.key like_regex "^(from_email|to_email|deleted_email'
                                '|provider|timestamp|ip|user_agent|subject_id)$"))'
                            ),
                        ),
                        _negated=True,
                    ),
                ),
                name="outboundmail_context_chk",
            ),
        ),
    ]
