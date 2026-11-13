from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar = [
        ("plants", "0005_plantconfiguration_mounting_geometry"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="EpiasConnection",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("provider_plant_id", models.PositiveBigIntegerField()),
                ("eic", models.CharField(max_length=100)),
                ("name", models.CharField(max_length=300)),
                ("enabled", models.BooleanField(default=True)),
                ("revision", models.PositiveIntegerField(default=1)),
                (
                    "meter_basis",
                    models.CharField(
                        choices=[
                            ("unknown", "unknown"),
                            ("net_export", "net_export"),
                            ("ac_generation", "ac_generation"),
                        ],
                        default="unknown",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "connected_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "plant",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, to="plants.plant"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="EpiasCollectionJob",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("connection_revision", models.PositiveIntegerField()),
                ("provider_plant", models.JSONField()),
                ("target_day", models.DateField()),
                ("scheduled_for", models.DateTimeField()),
                ("status", models.CharField(default="pending", max_length=20)),
                ("lease_token", models.UUIDField(null=True)),
                ("lease_expires_at", models.DateTimeField(null=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("next_attempt_at", models.DateTimeField()),
                ("last_error", models.CharField(default="", max_length=40)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "configuration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="plants.plantconfiguration"
                    ),
                ),
                (
                    "connection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="metering.epiasconnection"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="EpiasSnapshot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("attempt", models.PositiveSmallIntegerField()),
                ("target_day", models.DateField()),
                ("collected_at", models.DateTimeField()),
                ("available_at", models.DateTimeField()),
                ("raw_payload", models.JSONField()),
                ("readings", models.JSONField()),
                ("content_sha256", models.CharField(max_length=64)),
                ("complete", models.BooleanField(default=False)),
                (
                    "configuration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="plants.plantconfiguration"
                    ),
                ),
                (
                    "connection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="metering.epiasconnection"
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="metering.epiascollectionjob",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="epiascollectionjob",
            index=models.Index(
                fields=["status", "next_attempt_at"], name="metering_ep_status_11e228_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="epiascollectionjob",
            constraint=models.UniqueConstraint(
                fields=(
                    "connection",
                    "connection_revision",
                    "configuration",
                    "target_day",
                    "scheduled_for",
                ),
                name="epias_unique_collection_slot",
            ),
        ),
        migrations.AddIndex(
            model_name="epiassnapshot",
            index=models.Index(
                fields=["connection", "target_day", "available_at"],
                name="metering_ep_connect_df32e3_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="epiassnapshot",
            constraint=models.UniqueConstraint(
                fields=("job", "attempt"), name="epias_unique_job_acquisition"
            ),
        ),
    ]
