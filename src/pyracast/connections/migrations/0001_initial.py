from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="ProviderAccount",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("provider", models.CharField(max_length=30)),
                ("external_account_id", models.CharField(max_length=200)),
                ("encrypted_api_key", models.CharField(blank=True, default="", max_length=2048)),
                ("daily_quota", models.PositiveIntegerField(default=10)),
                ("enabled", models.BooleanField(default=True)),
                ("credential_revision", models.PositiveIntegerField(default=1)),
                ("request_blocked_until", models.DateTimeField(null=True)),
                ("authentication_failed_revision", models.PositiveIntegerField(null=True)),
                ("authorization_reference", models.CharField(default="", max_length=500)),
                ("authorized_at", models.DateTimeField(null=True)),
                (
                    "authorized_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="SiteBinding",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("region_revision", models.PositiveIntegerField()),
                ("site_id", models.CharField(max_length=200)),
                ("capacity_kw", models.FloatField()),
                ("dc_capacity_kw", models.FloatField(blank=True, null=True)),
                ("power_basis", models.CharField(default="ac", max_length=2)),
                ("capacity_semantics_verified", models.BooleanField(default=False)),
                ("tilt_degrees", models.FloatField(default=0)),
                ("azimuth_degrees", models.FloatField(default=0)),
                ("losses_percent", models.FloatField(default=0)),
                ("installation_date", models.DateField(null=True)),
                ("settings_verification_reference", models.CharField(default="", max_length=500)),
                ("latitude", models.FloatField()),
                ("longitude", models.FloatField()),
                ("timezone", models.CharField(max_length=100)),
                ("validated_at", models.DateTimeField(null=True)),
                ("validated_credential_revision", models.PositiveIntegerField(null=True)),
                ("health", models.CharField(default="unknown", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "provider_account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="connections.provideraccount",
                    ),
                ),
            ],
        ),
    ]
