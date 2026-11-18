from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast.db import RenameInPlace


class Migration(migrations.Migration):
    dependencies: ClassVar = [("notifications", "0004_recipient_and_time_shape_checks")]

    operations: ClassVar[list[Operation]] = [
        RenameInPlace(
            model_name="notificationsettings",
            old_name="singleton_notification_settings",
            item=models.CheckConstraint(
                condition=models.Q(("pk", 1)), name="notificationsettings_id_chk"
            ),
        ),
        RenameInPlace(
            model_name="notificationsettings",
            old_name="positive_notification_recipient_limit",
            item=models.CheckConstraint(
                condition=models.Q(("max_recipients__gte", 1)),
                name="notificationsettings_max_recipients_chk",
            ),
        ),
        RenameInPlace(
            model_name="reminderschedule",
            old_name="one_reminder_schedule_per_region",
            item=models.UniqueConstraint(fields=("region",), name="reminderschedule_region_uniq"),
        ),
        RenameInPlace(
            model_name="reminderschedule",
            old_name="reminder_schedule_language_valid",
            item=models.CheckConstraint(
                condition=models.Q(("language__in", ["en", "tr"])),
                name="reminderschedule_language_chk",
            ),
        ),
        RenameInPlace(
            model_name="notificationdelivery",
            old_name="unique_reminder_group_slot",
            item=models.UniqueConstraint(
                fields=("schedule", "scheduled_for"),
                name="notificationdelivery_schedule_scheduled_for_uniq",
            ),
        ),
        RenameInPlace(
            model_name="notificationdelivery",
            old_name="notification_delivery_language_valid",
            item=models.CheckConstraint(
                condition=models.Q(("language__in", ["en", "tr"])),
                name="notificationdelivery_language_chk",
            ),
        ),
        RenameInPlace(
            model_name="notificationdelivery",
            old_name="notification_delivery_status_valid",
            item=models.CheckConstraint(
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
                name="notificationdelivery_status_chk",
            ),
        ),
        RenameInPlace(
            model_name="notificationdelivery",
            old_name="notification_delivery_sending_prepared",
            item=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "sending"), _negated=True),
                    ("prepared_message__isnull", False),
                    _connector="OR",
                ),
                name="notificationdelivery_prepared_message_chk",
            ),
        ),
        RenameInPlace(
            model_name="notificationdelivery",
            old_name="notification_dispatch_ready",
            item=models.Index(
                condition=models.Q(("status__in", ("pending", "retry", "sending"))),
                fields=["available_at", "id"],
                name="notificationdelivery_due_idx",
            ),
        ),
    ]
