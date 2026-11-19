from typing import ClassVar

import django.db.models.deletion
import django.db.models.lookups
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import pyracast.db


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("accounts", "0007_invitation_token_minted_at_send"),
        ("notifications", "0005_schema_naming_convention"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="OutboundMail",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "kind",
                    models.CharField(
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
                            ("mfa/email/recovery_codes_generated", "Recovery codes generated"),
                            ("socialaccount/email/account_connected", "Sign-in provider connected"),
                            (
                                "socialaccount/email/account_disconnected",
                                "Sign-in provider disconnected",
                            ),
                            ("account/email/unknown_account", "Account not found"),
                            ("account/email/account_already_exists", "Account already exists"),
                            ("account/email/login_locked", "Password sign-in paused"),
                        ],
                        max_length=48,
                    ),
                ),
                ("recipient", models.EmailField(max_length=320)),
                (
                    "language",
                    models.CharField(choices=[("en", "English"), ("tr", "Türkçe")], max_length=2),
                ),
                ("owner_language", models.BooleanField(default=False)),
                ("context", models.JSONField(default=dict)),
                ("sealed_code", models.TextField(blank=True, default="")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("retry", "Retry"),
                            ("sending", "Sending"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                            ("uncertain", "Uncertain"),
                            ("skipped", "Skipped"),
                        ],
                        default="pending",
                        max_length=9,
                    ),
                ),
                ("created_at", models.DateTimeField()),
                ("available_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("published_at", models.DateTimeField(null=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("lease_token", models.UUIDField(null=True)),
                ("lease_expires_at", models.DateTimeField(null=True)),
                ("transmit_started_at", models.DateTimeField(null=True)),
                ("finished_at", models.DateTimeField(null=True)),
                (
                    "last_error",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("expired", "Expired before sending"),
                            ("budget_exhausted", "Hourly budget exhausted"),
                            ("invitation_unavailable", "Invitation closed"),
                            ("no_account", "No account"),
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
            ],
        ),
        migrations.AddField(
            model_name="outboundmail",
            name="invitation",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="accounts.workspaceinvitation",
            ),
        ),
        migrations.AddIndex(
            model_name="outboundmail",
            index=models.Index(
                condition=models.Q(("status__in", ("pending", "retry", "sending"))),
                fields=["available_at", "id"],
                name="outboundmail_due_idx",
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
                            "mfa/email/recovery_codes_generated",
                            "socialaccount/email/account_connected",
                            "socialaccount/email/account_disconnected",
                            "account/email/unknown_account",
                            "account/email/account_already_exists",
                            "account/email/login_locked",
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
                        "status__in",
                        ["pending", "retry", "sending", "sent", "failed", "uncertain", "skipped"],
                    )
                ),
                name="outboundmail_status_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmail",
            constraint=models.CheckConstraint(
                condition=models.Q(("language__in", ["en", "tr"])), name="outboundmail_language_chk"
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
                    models.Q(
                        ("lease_expires_at__isnull", False),
                        ("lease_token__isnull", False),
                        ("status__in", ["sending", "uncertain"]),
                    ),
                    models.Q(
                        ("lease_expires_at__isnull", True),
                        ("lease_token__isnull", True),
                        models.Q(("status__in", ["sending"]), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="outboundmail_lease_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmail",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("sealed_code", ""),
                    models.Q(
                        (
                            "kind__in",
                            (
                                "account/email/email_confirmation_signup",
                                "account/email/email_confirmation",
                                "account/email/password_reset_code",
                                "account/email/login_code",
                            ),
                        ),
                        ("status__in", ("pending", "retry", "sending")),
                    ),
                    _connector="OR",
                ),
                name="outboundmail_sealed_code_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmail",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("invitation__isnull", False), ("kind", "workspace_invitation")),
                    models.Q(
                        models.Q(("kind", "workspace_invitation"), _negated=True),
                        ("invitation__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="outboundmail_invitation_chk",
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
                                '|provider|timestamp|ip|user_agent)$"))'
                            ),
                        ),
                        _negated=True,
                    ),
                ),
                name="outboundmail_context_chk",
            ),
        ),
    ]
