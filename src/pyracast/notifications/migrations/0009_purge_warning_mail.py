from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("notifications", "0008_review_and_role_mail"),
    ]

    # Adds the purge warning kind; every stored row keeps its value.
    operations: ClassVar[list[Operation]] = [
        migrations.RemoveConstraint(
            model_name="outboundmail",
            name="outboundmail_kind_chk",
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
                    ("plant_purge_warning", "Plant deletion warning"),
                ],
                max_length=48,
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
                            "plant_purge_warning",
                        ],
                    )
                ),
                name="outboundmail_kind_chk",
            ),
        ),
    ]
