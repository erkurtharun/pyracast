import datetime
from typing import ClassVar

import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
import django.db.models.deletion
import django.db.models.expressions
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import pyracast.plants.models
from pyracast import schema_objects


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
            name="CalibrationActivation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="CalibrationRun",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("window_start", models.DateField()),
                ("window_end", models.DateField()),
                ("cutoff_at", models.DateTimeField()),
                ("observation_id_cutoff", models.PositiveBigIntegerField(null=True)),
                ("production_snapshot_ids", models.JSONField(default=list)),
                (
                    "measurement_selection_policy",
                    models.CharField(
                        default="manual-over-settlement-over-provisional-hour-v2", max_length=80
                    ),
                ),
                ("reference_snapshot_ids", models.JSONField(default=None, null=True)),
                ("incident_revision_ids", models.JSONField(default=list)),
                ("input_fingerprint", models.CharField(default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "pending"),
                            ("running", "running"),
                            ("ready", "ready"),
                            ("insufficient", "insufficient"),
                            ("rejected", "rejected"),
                            ("failed", "failed"),
                            ("superseded", "superseded"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("assessment", models.CharField(default="", max_length=200)),
                ("factor", models.FloatField(null=True)),
                ("result", models.JSONField(default=dict)),
                ("dataset_manifest", models.JSONField(default=dict)),
                ("dataset_sha256", models.CharField(default="", max_length=64)),
                ("lease_token", models.UUIDField(null=True)),
                ("lease_expires_at", models.DateTimeField(null=True)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("last_error", models.CharField(default="", max_length=200)),
                (
                    "binding",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="connections.sitebinding"
                    ),
                ),
                (
                    "comparison_activation",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="comparison_runs",
                        to="plants.calibrationactivation",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="calibrationactivation",
            name="calibration",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT, to="plants.calibrationrun"
            ),
        ),
        migrations.CreateModel(
            name="Plant",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("automatic_calibration_enabled", models.BooleanField(default=False)),
                ("next_calibration_check_at", models.DateTimeField(db_index=True, null=True)),
                (
                    "automatic_calibration_enabled_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "region",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="regions.region"
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="calibrationrun",
            name="plant",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="plants.plant"),
        ),
        migrations.AddField(
            model_name="calibrationactivation",
            name="plant",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="plants.plant"),
        ),
        migrations.CreateModel(
            name="PlantConfiguration",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("revision", models.PositiveIntegerField()),
                ("dc_capacity_mw", models.FloatField()),
                ("inverter_capacity_mw", models.FloatField(null=True)),
                ("export_limit_mw", models.FloatField(null=True)),
                (
                    "mounting_type",
                    models.CharField(
                        choices=[
                            ("unknown", "Unknown"),
                            ("fixed", "Fixed"),
                            ("tracking", "Tracking"),
                        ],
                        default="unknown",
                        max_length=10,
                    ),
                ),
                ("surface_tilt", models.FloatField(null=True)),
                ("surface_azimuth", models.FloatField(null=True)),
                (
                    "measurement_basis",
                    models.CharField(
                        choices=[("net_export", "Net export"), ("ac_generation", "AC generation")],
                        default="net_export",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "binding",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="connections.sitebinding"
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "plant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="configurations",
                        to="plants.plant",
                    ),
                ),
                (
                    "region",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="regions.region"
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="plant",
            name="current_configuration",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="plants.plantconfiguration",
            ),
        ),
        migrations.CreateModel(
            name="ForecastIssuance",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("target_date", models.DateField()),
                ("published_at", models.DateTimeField()),
                ("cutoff_at", models.DateTimeField()),
                ("model_version", models.CharField(max_length=100)),
                ("factor", models.FloatField()),
                ("payloads", models.JSONField()),
                ("outage_scenarios", models.JSONField()),
                ("native_intervals", models.JSONField()),
                (
                    "calibration",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="plants.calibrationrun",
                    ),
                ),
                (
                    "snapshot",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="forecasts.forecastsnapshot"
                    ),
                ),
                (
                    "plant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="plants.plant"
                    ),
                ),
                (
                    "configuration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="plants.plantconfiguration"
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="calibrationrun",
            name="configuration",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="plants.plantconfiguration"
            ),
        ),
        migrations.AddField(
            model_name="calibrationactivation",
            name="configuration",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="plants.plantconfiguration"
            ),
        ),
        migrations.CreateModel(
            name="PlantEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("action", models.CharField(max_length=50)),
                ("details", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "plant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="plants.plant"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="PlantSubmission",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("dc_capacity_mw", models.FloatField()),
                ("inverter_capacity_mw", models.FloatField(null=True)),
                ("export_limit_mw", models.FloatField(null=True)),
                (
                    "measurement_basis",
                    models.CharField(
                        choices=[("net_export", "Net export"), ("ac_generation", "AC generation")],
                        default="net_export",
                        max_length=20,
                    ),
                ),
                (
                    "mounting_type",
                    models.CharField(
                        choices=[
                            ("unknown", "Unknown"),
                            ("fixed", "Fixed"),
                            ("tracking", "Tracking"),
                        ],
                        default="unknown",
                        max_length=10,
                    ),
                ),
                ("surface_tilt", models.FloatField(null=True)),
                ("surface_azimuth", models.FloatField(null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "plant",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="submission",
                        to="plants.plant",
                    ),
                ),
                (
                    "submitted_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ProductionObservation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("starts_at", models.DateTimeField()),
                ("ends_at", models.DateTimeField()),
                ("energy_mwh", models.FloatField()),
                (
                    "quality",
                    models.CharField(
                        choices=[
                            ("valid", "Valid"),
                            ("outage", "Outage"),
                            ("curtailed", "Curtailed"),
                            ("invalid", "Invalid"),
                        ],
                        default="valid",
                        max_length=20,
                    ),
                ),
                ("source", models.CharField(max_length=200)),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                ("is_current", models.BooleanField(default=True)),
                ("is_reverted", models.BooleanField(default=False)),
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
                    "recorded_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "supersedes",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="replacement",
                        to="plants.productionobservation",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="plantconfiguration",
            constraint=models.UniqueConstraint(
                fields=("plant", "revision"), name="plant_config_revision_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="plantconfiguration",
            constraint=models.UniqueConstraint(
                fields=("id", "plant"), name="plant_config_id_plant_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="plantconfiguration",
            constraint=models.UniqueConstraint(
                fields=("id", "binding"), name="plant_config_id_binding_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="plantconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(("revision__gte", 1)), name="plant_config_revision_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="plantconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(("measurement_basis__in", ("net_export", "ac_generation"))),
                name="plant_measurement_basis_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(("dc_capacity_mw__gt", 0), ("dc_capacity_mw__lt", float("inf"))),
                name="plant_dc_capacity_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("inverter_capacity_mw__isnull", True),
                    models.Q(
                        ("inverter_capacity_mw__gt", 0), ("inverter_capacity_mw__lt", float("inf"))
                    ),
                    _connector="OR",
                ),
                name="plant_inverter_capacity_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("export_limit_mw__isnull", True),
                    models.Q(("export_limit_mw__gt", 0), ("export_limit_mw__lt", float("inf"))),
                    _connector="OR",
                ),
                name="plant_export_limit_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(("mounting_type__in", ("unknown", "fixed", "tracking"))),
                name="plant_mounting_type_supported",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("surface_tilt__isnull", True),
                    models.Q(("surface_tilt__gte", 0), ("surface_tilt__lte", 90)),
                    _connector="OR",
                ),
                name="plant_surface_tilt_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("surface_azimuth__isnull", True),
                    models.Q(("surface_azimuth__gte", 0), ("surface_azimuth__lt", 360)),
                    _connector="OR",
                ),
                name="plant_surface_azimuth_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("mounting_type", "fixed"),
                    models.Q(("surface_azimuth__isnull", True), ("surface_tilt__isnull", True)),
                    _connector="OR",
                ),
                name="plant_angles_require_fixed",
            ),
        ),
        migrations.AddConstraint(
            model_name="plant",
            constraint=models.UniqueConstraint(
                fields=("id", "region"), name="plant_region_identity"
            ),
        ),
        migrations.AddConstraint(
            model_name="plant",
            constraint=models.UniqueConstraint(fields=("region",), name="one_plant_per_region"),
        ),
        migrations.AddIndex(
            model_name="forecastissuance",
            index=models.Index(fields=["plant", "target_date"], name="issuance_plant_day"),
        ),
        migrations.AddConstraint(
            model_name="forecastissuance",
            constraint=models.UniqueConstraint(
                fields=("plant", "snapshot", "target_date"), name="forecast_issuance_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="forecastissuance",
            constraint=models.CheckConstraint(
                condition=models.Q(("factor__gt", 0), ("factor__lt", float("inf"))),
                name="forecast_issuance_factor_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="forecastissuance",
            constraint=models.CheckConstraint(
                condition=models.Q(("published_at__lt", models.F("cutoff_at"))),
                name="forecast_issuance_before_daylight",
            ),
        ),
        migrations.AddConstraint(
            model_name="forecastissuance",
            constraint=models.CheckConstraint(
                condition=models.Q(("calibration__isnull", False), ("factor", 1), _connector="OR"),
                name="issuance_baseline_factor_identity",
            ),
        ),
        migrations.AddConstraint(
            model_name="calibrationrun",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ("pending", "running"))),
                fields=("plant",),
                name="plant_one_pending_calibration",
            ),
        ),
        migrations.AddConstraint(
            model_name="calibrationrun",
            constraint=models.UniqueConstraint(
                fields=("id", "configuration"), name="calibration_id_config_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="calibrationrun",
            constraint=models.CheckConstraint(
                condition=models.Q(("window_end__gte", models.F("window_start"))),
                name="calibration_window_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="calibrationrun",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("factor__isnull", True),
                    models.Q(("factor__gt", 0), ("factor__lt", float("inf"))),
                    _connector="OR",
                ),
                name="calibration_factor_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="calibrationrun",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "status__in",
                        (
                            "pending",
                            "running",
                            "ready",
                            "insufficient",
                            "rejected",
                            "failed",
                            "superseded",
                        ),
                    )
                ),
                name="calibration_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="calibrationactivation",
            constraint=models.UniqueConstraint(
                fields=("id", "configuration"), name="activation_id_config_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="plantsubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(("dc_capacity_mw__gt", 0), ("dc_capacity_mw__lt", float("inf"))),
                name="submission_dc_capacity_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantsubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("inverter_capacity_mw__isnull", True),
                    models.Q(
                        ("inverter_capacity_mw__gt", 0), ("inverter_capacity_mw__lt", float("inf"))
                    ),
                    _connector="OR",
                ),
                name="submission_inverter_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantsubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("export_limit_mw__isnull", True),
                    models.Q(("export_limit_mw__gt", 0), ("export_limit_mw__lt", float("inf"))),
                    _connector="OR",
                ),
                name="submission_export_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantsubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(("measurement_basis__in", ("net_export", "ac_generation"))),
                name="submission_measurement_basis",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantsubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(("mounting_type__in", ("unknown", "fixed", "tracking"))),
                name="submission_mounting_supported",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantsubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("surface_tilt__isnull", True),
                    models.Q(("surface_tilt__gte", 0), ("surface_tilt__lte", 90)),
                    _connector="OR",
                ),
                name="submission_tilt_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantsubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("surface_azimuth__isnull", True),
                    models.Q(("surface_azimuth__gte", 0), ("surface_azimuth__lt", 360)),
                    _connector="OR",
                ),
                name="submission_azimuth_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantsubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("mounting_type", "fixed"),
                    models.Q(("surface_azimuth__isnull", True), ("surface_tilt__isnull", True)),
                    _connector="OR",
                ),
                name="submission_angles_require_fixed",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionobservation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_current", True)),
                fields=("plant", "starts_at", "ends_at"),
                name="plant_observation_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionobservation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "ends_at",
                        django.db.models.expressions.CombinedExpression(
                            models.F("starts_at"),
                            "+",
                            models.Value(datetime.timedelta(seconds=1800)),
                        ),
                    ),
                    (
                        "ends_at",
                        django.db.models.expressions.CombinedExpression(
                            models.F("starts_at"),
                            "+",
                            models.Value(datetime.timedelta(seconds=3600)),
                        ),
                    ),
                    _connector="OR",
                ),
                name="plant_observation_duration",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionobservation",
            constraint=models.CheckConstraint(
                condition=models.Q(("energy_mwh__gte", 0), ("energy_mwh__lt", float("inf"))),
                name="plant_observation_energy_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionobservation",
            constraint=models.CheckConstraint(
                condition=models.Q(("quality__in", ("valid", "outage", "curtailed", "invalid"))),
                name="plant_observation_quality_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionobservation",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
                condition=models.Q(("is_current", True), ("is_reverted", False)),
                expressions=(
                    ("plant", "="),
                    (
                        pyracast.plants.models.TsTzRange(
                            "starts_at",
                            "ends_at",
                            django.contrib.postgres.fields.ranges.RangeBoundary(),
                        ),
                        "&&",
                    ),
                ),
                name="plant_observation_no_overlap",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionobservation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("is_reverted", False), ("supersedes__isnull", False), _connector="OR"
                ),
                name="plant_observation_revert_has_source",
            ),
        ),
        *schema_objects.operations("plants", "0001_initial"),
    ]
