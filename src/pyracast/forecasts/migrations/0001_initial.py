import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar = [
        ("connections", "0002_initial"),
        ("regions", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="CollectionJob",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("endpoint", models.CharField(default="forecasts", max_length=30)),
                ("scheduled_slot", models.DateTimeField()),
                ("target_date", models.DateField()),
                ("status", models.CharField(default="pending", max_length=20)),
                ("available_at", models.DateTimeField()),
                ("retry_until", models.DateTimeField(null=True)),
                ("lease_token", models.UUIDField(null=True)),
                ("lease_expires_at", models.DateTimeField(null=True)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("last_error", models.CharField(default="", max_length=40)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "binding",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="connections.sitebinding"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="CollectionSchedule",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("enabled", models.BooleanField(default=True)),
                ("times", models.JSONField(default=list)),
                ("effective_from", models.DateTimeField()),
                (
                    "region",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, to="regions.region"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="DispatcherStatus",
            fields=[
                (
                    "phase",
                    models.CharField(
                        choices=[
                            ("collection", "Collection"),
                            ("notifications", "Notifications"),
                            ("calibration", "Calibration"),
                            ("retention", "Retention"),
                            ("metering", "Metering"),
                        ],
                        max_length=20,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("attempted_at", models.DateTimeField()),
                ("succeeded_at", models.DateTimeField(null=True)),
                ("last_error", models.CharField(blank=True, default="", max_length=100)),
                ("duration_ms", models.PositiveIntegerField(null=True)),
                ("published", models.PositiveIntegerField(null=True)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "phase__in",
                                (
                                    "collection",
                                    "notifications",
                                    "calibration",
                                    "retention",
                                    "metering",
                                ),
                            )
                        ),
                        name="dispatcher_phase_valid",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="ForecastSnapshot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("raw_payload", models.JSONField()),
                ("normalized_intervals", models.JSONField()),
                ("collected_at", models.DateTimeField()),
                ("issued_at", models.DateTimeField(null=True)),
                ("source_kind", models.CharField(default="unknown", max_length=20)),
                ("coverage_start", models.DateTimeField(db_index=True, null=True)),
                ("coverage_end", models.DateTimeField(db_index=True, null=True)),
                ("available_at", models.DateTimeField(null=True)),
                ("weather_payload", models.JSONField(null=True)),
                ("weather_error", models.CharField(default="", max_length=64)),
                (
                    "binding",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="snapshots",
                        to="connections.sitebinding",
                    ),
                ),
                (
                    "job",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT, to="forecasts.collectionjob"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ForecastResult",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("payload", models.JSONField()),
                ("daily_payloads", models.JSONField(default=dict)),
                ("published_at", models.DateTimeField()),
                (
                    "snapshot",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT, to="forecasts.forecastsnapshot"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="HistoricalReferenceImport",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("sha256", models.CharField(max_length=64)),
                ("source_name", models.CharField(max_length=255)),
                ("source_settings", models.JSONField()),
                ("imported_at", models.DateTimeField()),
                ("interval_count", models.PositiveIntegerField()),
                (
                    "binding",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="connections.sitebinding"
                    ),
                ),
                (
                    "imported_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="collectionjob",
            name="reference_import",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="forecasts.historicalreferenceimport",
            ),
        ),
        migrations.CreateModel(
            name="ReferenceCapacityNormalization",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("source_capacity_kw", models.FloatField()),
                ("target_capacity_kw", models.FloatField()),
                ("factor", models.FloatField()),
                ("authorization_reference", models.CharField(max_length=1000)),
                ("created_at", models.DateTimeField()),
                (
                    "authorized_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "snapshot",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="capacity_normalization",
                        to="forecasts.forecastsnapshot",
                    ),
                ),
                (
                    "source_snapshot",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="normalized_copies",
                        to="forecasts.forecastsnapshot",
                    ),
                ),
                (
                    "target_binding",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="connections.sitebinding"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="RequestAttempt",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("utc_day", models.DateField()),
                ("created_at", models.DateTimeField()),
                ("purpose", models.CharField(max_length=20)),
                ("outcome", models.CharField(default="uncertain", max_length=20)),
                ("correlation_id", models.UUIDField(default=uuid.uuid4, unique=True)),
                (
                    "binding",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="connections.sitebinding",
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="forecasts.collectionjob",
                    ),
                ),
                (
                    "provider_account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="connections.provideraccount",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="forecastsnapshot",
            index=models.Index(
                fields=["binding", "source_kind", "-collected_at", "-available_at", "-id"],
                name="snapshot_binding_source_age",
            ),
        ),
        migrations.AddConstraint(
            model_name="forecastsnapshot",
            constraint=models.UniqueConstraint(
                fields=("id", "binding"), name="snapshot_binding_identity"
            ),
        ),
        migrations.AddConstraint(
            model_name="forecastsnapshot",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("coverage_end__isnull", True), ("coverage_start__isnull", True)),
                    models.Q(
                        ("coverage_end__gt", models.F("coverage_start")),
                        ("coverage_end__isnull", False),
                        ("coverage_start__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="snapshot_coverage_bounds",
            ),
        ),
        migrations.AddConstraint(
            model_name="forecastsnapshot",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("source_kind__in", ("unknown", "forecast", "estimated_actuals"))
                ),
                name="snapshot_source_kind",
            ),
        ),
        migrations.AddConstraint(
            model_name="historicalreferenceimport",
            constraint=models.UniqueConstraint(
                fields=("id", "binding"), name="reference_import_binding_identity"
            ),
        ),
        migrations.AddConstraint(
            model_name="historicalreferenceimport",
            constraint=models.UniqueConstraint(
                fields=("binding", "sha256"), name="unique_historical_reference_import"
            ),
        ),
        migrations.AddIndex(
            model_name="collectionjob",
            index=models.Index(
                condition=models.Q(("status__in", ("pending", "retry", "running"))),
                fields=["available_at", "id"],
                name="collection_dispatch_due",
            ),
        ),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.UniqueConstraint(
                fields=("id", "binding"), name="collection_job_binding_identity"
            ),
        ),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ("pending", "running", "retry", "succeeded", "failed", "stale"))
                ),
                name="collection_job_status",
            ),
        ),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.UniqueConstraint(
                condition=models.Q(("reference_import__isnull", True)),
                fields=("binding", "endpoint", "scheduled_slot"),
                name="unique_collection_slot",
            ),
        ),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.UniqueConstraint(
                fields=("reference_import", "target_date"), name="unique_import_reference_day"
            ),
        ),
        migrations.AddConstraint(
            model_name="referencecapacitynormalization",
            constraint=models.CheckConstraint(
                condition=models.Q(("snapshot", models.F("source_snapshot")), _negated=True),
                name="normalization_distinct_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="referencecapacitynormalization",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("source_capacity_kw__gt", 0), ("source_capacity_kw__lt", float("inf"))
                ),
                name="normalization_source_capacity",
            ),
        ),
        migrations.AddConstraint(
            model_name="referencecapacitynormalization",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("target_capacity_kw__gt", 0), ("target_capacity_kw__lt", float("inf"))
                ),
                name="normalization_target_capacity",
            ),
        ),
        migrations.AddConstraint(
            model_name="referencecapacitynormalization",
            constraint=models.UniqueConstraint(
                fields=("source_snapshot", "target_binding"),
                name="unique_reference_capacity_normalization",
            ),
        ),
        migrations.AddConstraint(
            model_name="referencecapacitynormalization",
            constraint=models.CheckConstraint(
                condition=models.Q(("factor__gt", 0), ("factor__lt", float("inf"))),
                name="finite_positive_reference_scale",
            ),
        ),
        migrations.AddIndex(
            model_name="requestattempt",
            index=models.Index(fields=["provider_account", "utc_day"], name="request_account_day"),
        ),
        migrations.AddConstraint(
            model_name="requestattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("job__isnull", True), ("binding__isnull", False), _connector="OR"
                ),
                name="request_job_requires_binding",
            ),
        ),
        migrations.AddConstraint(
            model_name="requestattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(("outcome__in", ("uncertain", "succeeded", "failed"))),
                name="request_attempt_outcome",
            ),
        ),
        *schema_objects.operations("forecasts", "0001_initial"),
    ]
