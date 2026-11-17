from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar = [
        ("plants", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
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
                ("settlement_plant_id", models.PositiveBigIntegerField(null=True)),
                ("settlement_checked_at", models.DateTimeField(null=True)),
                ("eic", models.CharField(max_length=100)),
                ("name", models.CharField(max_length=300)),
                ("enabled", models.BooleanField(default=True)),
                ("revision", models.PositiveIntegerField(default=1)),
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
                        on_delete=django.db.models.deletion.PROTECT, to="plants.plant"
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
                ("period_end_day", models.DateField(null=True)),
                (
                    "source_kind",
                    models.CharField(
                        choices=[
                            ("realtime_generation", "Real-time generation"),
                            ("settlement_injection", "Settlement injection"),
                        ],
                        default="realtime_generation",
                        max_length=30,
                    ),
                ),
                ("scheduled_for", models.DateTimeField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                            ("superseded", "Superseded"),
                            ("awaiting_publication", "Awaiting publication"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("lease_token", models.UUIDField(null=True)),
                ("lease_expires_at", models.DateTimeField(null=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("next_attempt_at", models.DateTimeField()),
                ("retry_until", models.DateTimeField(null=True)),
                ("last_error", models.CharField(default="", max_length=40)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "configuration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="plants.plantconfiguration"
                    ),
                ),
                (
                    "plant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="plants.plant"
                    ),
                ),
                (
                    "connection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="metering.epiasconnection"
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
                (
                    "source_kind",
                    models.CharField(
                        choices=[
                            ("realtime_generation", "Real-time generation"),
                            ("settlement_injection", "Settlement injection"),
                        ],
                        default="realtime_generation",
                        max_length=30,
                    ),
                ),
                ("provider_plant", models.JSONField(default=dict)),
                ("endpoint", models.URLField(default="")),
                (
                    "normalization_policy",
                    models.CharField(default="hour-start-istanbul-v1", max_length=40),
                ),
                ("collected_at", models.DateTimeField()),
                ("available_at", models.DateTimeField()),
                ("raw_payload", models.JSONField()),
                ("readings", models.JSONField()),
                ("content_sha256", models.CharField(max_length=64)),
                ("complete", models.BooleanField(default=False)),
                (
                    "configuration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="plants.plantconfiguration"
                    ),
                ),
                (
                    "connection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="metering.epiasconnection"
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="metering.epiascollectionjob",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="epiasconnection",
            constraint=models.UniqueConstraint(
                fields=("id", "plant"), name="epias_connection_plant_identity"
            ),
        ),
        migrations.AddConstraint(
            model_name="epiasconnection",
            constraint=models.CheckConstraint(
                condition=models.Q(("revision__gte", 1)), name="epias_revision_positive"
            ),
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
                    "source_kind",
                    "target_day",
                    "scheduled_for",
                ),
                name="epias_unique_collection_slot",
            ),
        ),
        migrations.AddConstraint(
            model_name="epiascollectionjob",
            constraint=models.UniqueConstraint(
                fields=("id", "connection", "configuration"), name="epias_job_lineage_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="epiascollectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("period_end_day__isnull", True),
                    ("period_end_day__gte", models.F("target_day")),
                    _connector="OR",
                ),
                name="epias_job_period_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="epiascollectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(("connection_revision__gte", 1)),
                name="epias_job_revision_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="epiascollectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("source_kind__in", ["realtime_generation", "settlement_injection"])
                ),
                name="epias_job_source_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="epiascollectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "status__in",
                        [
                            "pending",
                            "running",
                            "succeeded",
                            "failed",
                            "superseded",
                            "awaiting_publication",
                        ],
                    )
                ),
                name="epias_job_status_valid",
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
                fields=("job", "attempt", "target_day"), name="epias_unique_job_acquisition"
            ),
        ),
        migrations.AddConstraint(
            model_name="epiassnapshot",
            constraint=models.CheckConstraint(
                condition=models.Q(("attempt__gte", 1)), name="epias_acquisition_attempt_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="epiassnapshot",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("source_kind__in", ["realtime_generation", "settlement_injection"])
                ),
                name="epias_acquisition_source_valid",
            ),
        ),
        *schema_objects.operations("metering", "0001_initial"),
    ]
