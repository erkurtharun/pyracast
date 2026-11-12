from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("notifications", "0005_remove_redundant_reminder_email"),
        ("forecasts", "0012_forecastsnapshot_binding_and_more"),
        ("regions", "0001_initial"),
    ]

    operations: ClassVar = [
        migrations.AddField(
            model_name="notificationdelivery",
            name="binding",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT, to="connections.sitebinding"
            ),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="preparation_deadline",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="prepared_message",
            field=models.JSONField(null=True),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="transmit_started_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunSQL(
            "UPDATE notifications_notificationdelivery d "
            "SET binding_id = COALESCE((SELECT binding_id FROM forecasts_forecastsnapshot "
            "WHERE id = d.snapshot_id), r.approved_binding_id), "
            "preparation_deadline = d.scheduled_for "
            "FROM notifications_reminderpreference p JOIN regions_region r ON r.id = p.region_id "
            "WHERE d.preference_id = p.id",
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "UPDATE notifications_notificationdelivery "
            "SET transmit_started_at = COALESCE(sent_at, scheduled_for) "
            "WHERE status IN ('sending', 'uncertain')",
            migrations.RunSQL.noop,
        ),
        # Flush backfill FK checks before the schema editor creates the binding index.
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", migrations.RunSQL.noop),
    ]
