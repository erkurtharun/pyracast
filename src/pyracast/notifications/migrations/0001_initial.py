import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = (
        ("forecasts", "0003_alter_requestattempt_binding"),
        ("regions", "0002_account_audit_targets"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    )

    operations = (
        migrations.CreateModel(
            name="ReminderPreference",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("enabled", models.BooleanField(default=False)),
                ("email", models.EmailField(blank=True, default="", max_length=254)),
                ("times", models.JSONField(default=list)),
                ("effective_from", models.DateTimeField()),
                (
                    "region",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="regions.region"
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
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
                ("status", models.CharField(default="pending", max_length=20)),
                ("available_at", models.DateTimeField()),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("lease_token", models.UUIDField(null=True)),
                ("lease_expires_at", models.DateTimeField(null=True)),
                ("recipient", models.EmailField(blank=True, default="", max_length=254)),
                ("sent_at", models.DateTimeField(null=True)),
                ("last_error", models.CharField(default="", max_length=40)),
                (
                    "snapshot",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="forecasts.forecastsnapshot",
                    ),
                ),
                (
                    "preference",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="notifications.reminderpreference",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="reminderpreference",
            constraint=models.UniqueConstraint(
                fields=("user", "region"), name="unique_user_region_reminder"
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.UniqueConstraint(
                fields=("preference", "scheduled_for"), name="unique_reminder_delivery_slot"
            ),
        ),
    )
