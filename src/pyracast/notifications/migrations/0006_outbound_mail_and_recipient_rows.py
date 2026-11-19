import sys
from collections import defaultdict
from typing import ClassVar

import django.db.models.deletion
import django.db.models.functions.text
import django.db.models.lookups
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation
from psycopg import sql

import pyracast.db

BIGINT_MAX = 2**63 - 1
# A lowered address longer than this cannot enter the unique B-tree index on lower(email).
INDEXABLE_BYTES = 2000


def _rows(
    apps: Apps, schema_editor: BaseDatabaseSchemaEditor, label: str
) -> models.QuerySet[models.Model]:
    app_label, model_name = label.split(".")
    stored: type[models.Model] = apps.get_model(app_label, model_name)
    return stored._default_manager.using(schema_editor.connection.alias).all()


def _addresses(
    schema_editor: BaseDatabaseSchemaEditor, model: type[models.Model]
) -> dict[int, list[tuple[str, str, int]]]:
    """Each list's addresses in order with the database's lower() and its size in bytes, so a
    repeat is judged exactly as the unique index judges it."""
    listed: dict[int, list[tuple[str, str, int]]] = defaultdict(list)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "SELECT stored.id, item.address, lower(item.address),"
                " octet_length(lower(item.address)) FROM {} AS stored"
                " CROSS JOIN LATERAL jsonb_array_elements_text(stored.recipients)"
                " WITH ORDINALITY AS item(address, position) ORDER BY stored.id, item.position"
            ).format(sql.Identifier(model._meta.db_table))
        )
        for pk, address, lowered, size in cursor.fetchall():
            listed[pk].append((address, lowered, size))
    return listed


def _membership(
    pins: list[object], tenant_id: int, members: dict[int, tuple[int, bool]]
) -> tuple[int | None, int | None]:
    """The pin an address keeps: a membership of the plant's workspace with an active user, as
    any one of them made the address eligible, else one that exists, else the first pin that
    can be a membership id. Returns (existing membership, kept id)."""
    ids = [int(pin) for pin in pins if isinstance(pin, int | float) and 1 <= pin <= BIGINT_MAX]
    active = [pk for pk in ids if members.get(pk) == (tenant_id, True)]
    existing = [pk for pk in ids if pk in members]
    return next(iter(active + existing), None), next(iter(active + existing + ids), None)


def copy_recipients_to_rows(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Every stored address becomes a row in its list's order, invalid ones included, and keeps
    the eligibility it had. Skipped and counted: a repeat of an address in the same list; a
    schedule address pinned only to removed memberships (never sent to; the membership's cascade
    removes such a row); an address too long to index, whose schedule is paused instead of
    sending without it. A delivery address pinned only to ids no membership can have stays
    refused as revoked."""
    members = {
        pk: (tenant_id, active)
        for pk, tenant_id, active in _rows(apps, schema_editor, "accounts.Membership").values_list(
            "pk", "tenant_id", "user__is_active"
        )
    }
    schedule_rows = _rows(apps, schema_editor, "notifications.ReminderRecipient")
    delivery_rows = _rows(apps, schema_editor, "notifications.DeliveryRecipient")
    removed = repeated = unindexable = 0
    held: set[int] = set()
    schedules = _rows(apps, schema_editor, "notifications.ReminderSchedule")
    listed = _addresses(schema_editor, schedules.model)
    for pk, pins, tenant_id in schedules.values_list(
        "pk", "recipient_memberships", "region__tenant_id"
    ).iterator():
        kept: list[models.Model] = []
        seen: set[str] = set()
        for address, lowered, size in listed[pk]:
            pinned = pins.get(address.casefold(), [])
            membership, _ = _membership(pinned, tenant_id, members)
            if pinned and membership is None:
                removed += 1
            elif lowered in seen:
                repeated += 1
            elif size > INDEXABLE_BYTES:
                unindexable += 1
                held.add(pk)
            else:
                seen.add(lowered)
                kept.append(
                    schedule_rows.model(schedule_id=pk, email=address, membership_id=membership)
                )
        schedule_rows.bulk_create(kept)
    schedules.filter(pk__in=held).update(enabled=False)
    deliveries = _rows(apps, schema_editor, "notifications.NotificationDelivery")
    listed = _addresses(schema_editor, deliveries.model)
    for pk, pins, revoked, tenant_id in deliveries.values_list(
        "pk", "recipient_memberships", "revoked_recipients", "schedule__region__tenant_id"
    ).iterator():
        frozen: list[models.Model] = []
        seen = set()
        for address, lowered, size in listed[pk]:
            pinned = pins.get(address.casefold(), [])
            _, membership = _membership(pinned, tenant_id, members)
            if lowered in seen:
                repeated += 1
            elif size > INDEXABLE_BYTES:
                unindexable += 1
            else:
                seen.add(lowered)
                frozen.append(
                    delivery_rows.model(
                        delivery_id=pk,
                        email=address,
                        membership_id=membership,
                        revoked=address.casefold() in revoked
                        or bool(pinned and membership is None),
                    )
                )
        delivery_rows.bulk_create(frozen)
    for count, what in (
        (removed, "reminder recipient(s) pinned only to removed memberships were not kept"),
        (repeated, "repeated recipient address(es) were kept once"),
        (unindexable, f"recipient address(es) over {INDEXABLE_BYTES} bytes were not kept"),
        (len(held), "reminder schedule(s) holding such an address were paused"),
    ):
        if count:
            sys.stdout.write(f"\n  notifications: {count} {what}.")


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("accounts", "0007_invitation_token_minted_at_send"),
        ("notifications", "0005_schema_naming_convention"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="DeliveryRecipient",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("email", models.CharField()),
                ("membership_id", models.PositiveBigIntegerField(null=True)),
                ("revoked", models.BooleanField(default=False)),
            ],
            options={
                "ordering": ("pk",),
            },
        ),
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
                ("recipient", models.EmailField(max_length=254)),
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
        migrations.CreateModel(
            name="ReminderRecipient",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("email", models.CharField()),
            ],
            options={
                "ordering": ("pk",),
            },
        ),
        migrations.RemoveConstraint(
            model_name="notificationdelivery",
            name="notificationdelivery_recipients_chk",
        ),
        migrations.RemoveConstraint(
            model_name="notificationdelivery",
            name="notificationdelivery_recipient_memberships_chk",
        ),
        migrations.RemoveConstraint(
            model_name="notificationdelivery",
            name="notificationdelivery_revoked_recipients_chk",
        ),
        migrations.RemoveConstraint(
            model_name="reminderschedule",
            name="reminderschedule_recipients_chk",
        ),
        migrations.RemoveConstraint(
            model_name="reminderschedule",
            name="reminderschedule_recipient_memberships_chk",
        ),
        migrations.AddField(
            model_name="deliveryrecipient",
            name="delivery",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="recipients",
                to="notifications.notificationdelivery",
            ),
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
        migrations.AddField(
            model_name="reminderrecipient",
            name="membership",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE, to="accounts.membership"
            ),
        ),
        migrations.AddField(
            model_name="reminderrecipient",
            name="schedule",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="recipients",
                to="notifications.reminderschedule",
            ),
        ),
        migrations.RunPython(copy_recipients_to_rows, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="notificationdelivery",
            name="recipient_memberships",
        ),
        migrations.RemoveField(
            model_name="notificationdelivery",
            name="recipients",
        ),
        migrations.RemoveField(
            model_name="notificationdelivery",
            name="revoked_recipients",
        ),
        migrations.RemoveField(
            model_name="reminderschedule",
            name="recipient_memberships",
        ),
        migrations.RemoveField(
            model_name="reminderschedule",
            name="recipients",
        ),
        migrations.AddConstraint(
            model_name="deliveryrecipient",
            constraint=models.UniqueConstraint(
                models.F("delivery"),
                django.db.models.functions.text.Lower("email"),
                name="deliveryrecipient_delivery_email_uniq",
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
        migrations.AddConstraint(
            model_name="reminderrecipient",
            constraint=models.UniqueConstraint(
                models.F("schedule"),
                django.db.models.functions.text.Lower("email"),
                name="reminderrecipient_schedule_email_uniq",
            ),
        ),
    ]
