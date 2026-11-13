import django.core.validators
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def preserve_recipients(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    settings = apps.get_model("notifications", "NotificationSettings")
    preference = apps.get_model("notifications", "ReminderPreference")
    settings.objects.using(schema_editor.connection.alias).get_or_create(pk=1)
    for row in (
        preference.objects.using(schema_editor.connection.alias).exclude(email="").iterator()
    ):
        row.recipients = [row.email]
        row.save(update_fields=["recipients"], using=schema_editor.connection.alias)


class Migration(migrations.Migration):
    dependencies = (
        ("forecasts", "0008_reference_capacity_normalization"),
        ("notifications", "0001_initial"),
    )

    operations = (
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
                            "Global distinct recipient limit per plant across all members. "
                            "Lowering it pauses reminders at plants exceeding the limit until "
                            "their recipient lists are revised."
                        ),
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
            ],
            options={
                "verbose_name": "email reminder settings",
                "verbose_name_plural": "email reminder settings",
            },
        ),
        migrations.RemoveConstraint(
            model_name="notificationdelivery",
            name="unique_reminder_delivery_slot",
        ),
        migrations.AddField(
            model_name="reminderpreference",
            name="recipients",
            field=models.JSONField(default=list),
        ),
        migrations.RunPython(preserve_recipients, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.UniqueConstraint(
                fields=("preference", "scheduled_for", "recipient"),
                name="unique_reminder_recipient_slot",
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationsettings",
            constraint=models.CheckConstraint(
                condition=models.Q(("pk", 1)), name="singleton_notification_settings"
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationsettings",
            constraint=models.CheckConstraint(
                condition=models.Q(("max_recipients__gte", 1)),
                name="positive_notification_recipient_limit",
            ),
        ),
    )
