import uuid
from typing import ClassVar

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar = [
        ("connections", "0002_initial"),
        ("forecasts", "0001_initial"),
        ("regions", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="NotificationSettings",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "max_recipients",
                    models.PositiveSmallIntegerField(
                        default=5,
                        help_text=(
                            "Global recipient limit per plant reminder schedule. Lowering "
                            "it pauses reminders at plants exceeding the limit until their "
                            "recipient lists are revised."
                        ),
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
            ],
            options={
                "verbose_name": "email reminder settings",
                "verbose_name_plural": "email reminder settings",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("pk", 1)), name="singleton_notification_settings"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("max_recipients__gte", 1)),
                        name="positive_notification_recipient_limit",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ReminderSchedule",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("enabled", models.BooleanField(default=False)),
                ("recipients", models.JSONField(default=list)),
                ("include_workspace_members", models.BooleanField(default=False)),
                ("recipient_memberships", models.JSONField(default=dict)),
                ("times", models.JSONField(default=list)),
                ("effective_from", models.DateTimeField()),
                (
                    "language",
                    models.CharField(
                        choices=[("en", "English"), ("tr", "Türkçe")], default="en", max_length=2
                    ),
                ),
                (
                    "configured_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "region",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="regions.region"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="NotificationDelivery",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("scheduled_for", models.DateTimeField()),
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
                            ("cancelled", "Cancelled"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("available_at", models.DateTimeField()),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("lease_token", models.UUIDField(null=True)),
                ("lease_expires_at", models.DateTimeField(null=True)),
                ("recipients", models.JSONField(default=list)),
                ("recipient_memberships", models.JSONField(default=dict)),
                ("revoked_recipients", models.JSONField(default=list)),
                (
                    "language",
                    models.CharField(
                        choices=[("en", "English"), ("tr", "Türkçe")], default="en", max_length=2
                    ),
                ),
                ("preparation_deadline", models.DateTimeField()),
                ("prepared_message", models.JSONField(null=True)),
                ("transmit_started_at", models.DateTimeField(null=True)),
                ("sent_at", models.DateTimeField(null=True)),
                ("last_error", models.CharField(default="", max_length=40)),
                (
                    "message_token",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                (
                    "binding",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="connections.sitebinding"
                    ),
                ),
                (
                    "snapshot",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="forecasts.forecastsnapshot",
                    ),
                ),
                (
                    "schedule",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="notifications.reminderschedule",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="reminderschedule",
            constraint=models.UniqueConstraint(
                fields=("region",), name="one_reminder_schedule_per_region"
            ),
        ),
        migrations.AddConstraint(
            model_name="reminderschedule",
            constraint=models.CheckConstraint(
                condition=models.Q(("language__in", ["en", "tr"])),
                name="reminder_schedule_language_valid",
            ),
        ),
        migrations.AddIndex(
            model_name="notificationdelivery",
            index=models.Index(
                condition=models.Q(("status__in", ("pending", "retry", "sending"))),
                fields=["available_at", "id"],
                name="notification_dispatch_ready",
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.UniqueConstraint(
                fields=("schedule", "scheduled_for"), name="unique_reminder_group_slot"
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(("language__in", ["en", "tr"])),
                name="notification_delivery_language_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "status__in",
                        [
                            "pending",
                            "retry",
                            "sending",
                            "sent",
                            "failed",
                            "uncertain",
                            "skipped",
                            "cancelled",
                        ],
                    )
                ),
                name="notification_delivery_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "sending"), _negated=True),
                    ("prepared_message__isnull", False),
                    _connector="OR",
                ),
                name="notification_delivery_sending_prepared",
            ),
        ),
    ]
