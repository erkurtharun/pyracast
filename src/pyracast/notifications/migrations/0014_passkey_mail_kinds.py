from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

MAIL_KINDS = {
    "workspace_invitation": "Workspace invitation",
    "account/email/email_confirmation_signup": "Sign-up code",
    "account/email/email_confirmation": "Verification code",
    "account/email/password_reset_code": "Password reset code",
    "account/email/login_code": "Confirmation code",
    "account/email/password_changed": "Password changed",
    "account/email/password_set": "Password set",
    "account/email/password_reset": "Password reset",
    "account/email/email_changed": "Email address changed",
    "account/email/email_confirm": "Email address verified",
    "account/email/email_deleted": "Email address removed",
    "mfa/email/totp_activated": "Two-factor authentication on",
    "mfa/email/totp_deactivated": "Two-factor authentication off",
    "mfa/email/webauthn_added": "Passkey added",
    "mfa/email/webauthn_removed": "Passkey removed",
    "account/email/second_factor_reset": "Second factor reset",
    "mfa/email/recovery_codes_generated": "Recovery codes generated",
    "socialaccount/email/account_connected": "Sign-in provider connected",
    "socialaccount/email/account_disconnected": "Sign-in provider disconnected",
    "account/email/unknown_account": "Account not found",
    "account/email/account_already_exists": "Account already exists",
    "account/email/login_locked": "Password sign-in paused",
    "plant_approved": "Plant approved",
    "plant_rejected": "Plant not approved",
    "plant_suspended": "Plant suspended",
    "workspace_admin_assigned": "Workspace administrator role received",
    "workspace_admin_released": "Workspace administrator role moved away",
    "plant_purge_warning": "Plant deletion warning",
}


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("accounts", "0009_workspace_administrator_names"),
        ("notifications", "0013_forecast_email_names"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RemoveConstraint(
            model_name="outboundmail",
            name="outboundmail_kind_chk",
        ),
        migrations.AlterField(
            model_name="outboundmail",
            name="kind",
            field=models.CharField(choices=list(MAIL_KINDS.items()), max_length=48),
        ),
        migrations.AddConstraint(
            model_name="outboundmail",
            constraint=models.CheckConstraint(
                condition=models.Q(kind__in=list(MAIL_KINDS)),
                name="outboundmail_kind_chk",
            ),
        ),
    ]
