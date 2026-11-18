import datetime
from typing import ClassVar

import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
import django.db.models.expressions
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import pyracast.plants.models
from pyracast import schema_objects
from pyracast.db import RenameInPlace


class Migration(migrations.Migration):
    dependencies: ClassVar = [("plants", "0003_lease_check_and_observation_trigger")]

    operations: ClassVar[list[Operation]] = [
        migrations.RenameField(
            model_name="plantsubmission", old_name="surface_tilt", new_name="surface_tilt_degrees"
        ),
        migrations.RenameField(
            model_name="plantsubmission",
            old_name="surface_azimuth",
            new_name="surface_azimuth_degrees",
        ),
        migrations.RenameField(
            model_name="plantconfiguration",
            old_name="surface_tilt",
            new_name="surface_tilt_degrees",
        ),
        migrations.RenameField(
            model_name="plantconfiguration",
            old_name="surface_azimuth",
            new_name="surface_azimuth_degrees",
        ),
        migrations.RenameField(
            model_name="productionobservation", old_name="recorded_at", new_name="created_at"
        ),
        migrations.RenameField(
            model_name="calibrationrun", old_name="input_fingerprint", new_name="input_sha256"
        ),
        RenameInPlace(
            model_name="plant",
            old_name="plant_region_identity",
            item=models.UniqueConstraint(fields=("id", "region"), name="plant_id_region_uniq"),
        ),
        RenameInPlace(
            model_name="plant",
            old_name="one_plant_per_region",
            item=models.UniqueConstraint(fields=("region",), name="plant_region_uniq"),
        ),
        RenameInPlace(
            model_name="plantsubmission",
            old_name="submission_dc_capacity_finite",
            item=models.CheckConstraint(
                condition=models.Q(("dc_capacity_mw__gt", 0), ("dc_capacity_mw__lt", float("inf"))),
                name="plantsubmission_dc_capacity_mw_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantsubmission",
            old_name="submission_inverter_finite",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("inverter_capacity_mw__isnull", True),
                    models.Q(
                        ("inverter_capacity_mw__gt", 0), ("inverter_capacity_mw__lt", float("inf"))
                    ),
                    _connector="OR",
                ),
                name="plantsubmission_inverter_capacity_mw_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantsubmission",
            old_name="submission_export_finite",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("export_limit_mw__isnull", True),
                    models.Q(("export_limit_mw__gt", 0), ("export_limit_mw__lt", float("inf"))),
                    _connector="OR",
                ),
                name="plantsubmission_export_limit_mw_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantsubmission",
            old_name="submission_measurement_basis",
            item=models.CheckConstraint(
                condition=models.Q(("measurement_basis__in", ["net_export", "ac_generation"])),
                name="plantsubmission_measurement_basis_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantsubmission",
            old_name="submission_mounting_supported",
            item=models.CheckConstraint(
                condition=models.Q(("mounting_type__in", ["unknown", "fixed", "tracking"])),
                name="plantsubmission_mounting_type_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantsubmission",
            old_name="submission_tilt_valid",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("surface_tilt_degrees__isnull", True),
                    models.Q(("surface_tilt_degrees__gte", 0), ("surface_tilt_degrees__lte", 90)),
                    _connector="OR",
                ),
                name="plantsubmission_surface_tilt_degrees_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantsubmission",
            old_name="submission_azimuth_valid",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("surface_azimuth_degrees__isnull", True),
                    models.Q(
                        ("surface_azimuth_degrees__gte", 0), ("surface_azimuth_degrees__lt", 360)
                    ),
                    _connector="OR",
                ),
                name="plantsubmission_surface_azimuth_degrees_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantsubmission",
            old_name="submission_angles_require_fixed",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("mounting_type", "fixed"),
                    models.Q(
                        ("surface_azimuth_degrees__isnull", True),
                        ("surface_tilt_degrees__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="plantsubmission_angles_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantconfiguration",
            old_name="plant_config_revision_unique",
            item=models.UniqueConstraint(
                fields=("plant", "revision"), name="plantconfiguration_plant_revision_uniq"
            ),
        ),
        RenameInPlace(
            model_name="plantconfiguration",
            old_name="plant_config_id_plant_unique",
            item=models.UniqueConstraint(
                fields=("id", "plant"), name="plantconfiguration_id_plant_uniq"
            ),
        ),
        RenameInPlace(
            model_name="plantconfiguration",
            old_name="plant_config_id_binding_unique",
            item=models.UniqueConstraint(
                fields=("id", "binding"), name="plantconfiguration_id_binding_uniq"
            ),
        ),
        RenameInPlace(
            model_name="plantconfiguration",
            old_name="plant_config_revision_positive",
            item=models.CheckConstraint(
                condition=models.Q(("revision__gte", 1)), name="plantconfiguration_revision_chk"
            ),
        ),
        RenameInPlace(
            model_name="plantconfiguration",
            old_name="plant_measurement_basis_valid",
            item=models.CheckConstraint(
                condition=models.Q(("measurement_basis__in", ["net_export", "ac_generation"])),
                name="plantconfiguration_measurement_basis_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantconfiguration",
            old_name="plant_dc_capacity_finite",
            item=models.CheckConstraint(
                condition=models.Q(("dc_capacity_mw__gt", 0), ("dc_capacity_mw__lt", float("inf"))),
                name="plantconfiguration_dc_capacity_mw_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantconfiguration",
            old_name="plant_inverter_capacity_finite",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("inverter_capacity_mw__isnull", True),
                    models.Q(
                        ("inverter_capacity_mw__gt", 0), ("inverter_capacity_mw__lt", float("inf"))
                    ),
                    _connector="OR",
                ),
                name="plantconfiguration_inverter_capacity_mw_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantconfiguration",
            old_name="plant_export_limit_finite",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("export_limit_mw__isnull", True),
                    models.Q(("export_limit_mw__gt", 0), ("export_limit_mw__lt", float("inf"))),
                    _connector="OR",
                ),
                name="plantconfiguration_export_limit_mw_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantconfiguration",
            old_name="plant_mounting_type_supported",
            item=models.CheckConstraint(
                condition=models.Q(("mounting_type__in", ["unknown", "fixed", "tracking"])),
                name="plantconfiguration_mounting_type_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantconfiguration",
            old_name="plant_surface_tilt_valid",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("surface_tilt_degrees__isnull", True),
                    models.Q(("surface_tilt_degrees__gte", 0), ("surface_tilt_degrees__lte", 90)),
                    _connector="OR",
                ),
                name="plantconfiguration_surface_tilt_degrees_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantconfiguration",
            old_name="plant_surface_azimuth_valid",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("surface_azimuth_degrees__isnull", True),
                    models.Q(
                        ("surface_azimuth_degrees__gte", 0), ("surface_azimuth_degrees__lt", 360)
                    ),
                    _connector="OR",
                ),
                name="plantconfiguration_surface_azimuth_degrees_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantconfiguration",
            old_name="plant_angles_require_fixed",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("mounting_type", "fixed"),
                    models.Q(
                        ("surface_azimuth_degrees__isnull", True),
                        ("surface_tilt_degrees__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="plantconfiguration_angles_chk",
            ),
        ),
        RenameInPlace(
            model_name="productionobservation",
            old_name="plant_observation_unique",
            item=models.UniqueConstraint(
                condition=models.Q(("is_current", True)),
                fields=("plant", "starts_at", "ends_at"),
                name="productionobservation_plant_starts_at_ends_at_uniq",
            ),
        ),
        RenameInPlace(
            model_name="productionobservation",
            old_name="plant_observation_duration",
            item=models.CheckConstraint(
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
                name="productionobservation_ends_at_chk",
            ),
        ),
        RenameInPlace(
            model_name="productionobservation",
            old_name="plant_observation_energy_finite",
            item=models.CheckConstraint(
                condition=models.Q(("energy_mwh__gte", 0), ("energy_mwh__lt", float("inf"))),
                name="productionobservation_energy_mwh_chk",
            ),
        ),
        RenameInPlace(
            model_name="productionobservation",
            old_name="plant_observation_quality_valid",
            item=models.CheckConstraint(
                condition=models.Q(("quality__in", ["valid", "outage", "curtailed", "invalid"])),
                name="productionobservation_quality_chk",
            ),
        ),
        RenameInPlace(
            model_name="productionobservation",
            old_name="plant_observation_no_overlap",
            item=django.contrib.postgres.constraints.ExclusionConstraint(
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
                name="productionobservation_plant_interval_excl",
            ),
        ),
        RenameInPlace(
            model_name="productionobservation",
            old_name="plant_observation_revert_has_source",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("is_reverted", False), ("supersedes__isnull", False), _connector="OR"
                ),
                name="productionobservation_is_reverted_chk",
            ),
        ),
        RenameInPlace(
            model_name="calibrationrun",
            old_name="plant_one_pending_calibration",
            item=models.UniqueConstraint(
                condition=models.Q(("status__in", ("pending", "running"))),
                fields=("plant",),
                name="calibrationrun_plant_uniq",
            ),
        ),
        RenameInPlace(
            model_name="calibrationrun",
            old_name="calibration_id_config_unique",
            item=models.UniqueConstraint(
                fields=("id", "configuration"), name="calibrationrun_id_configuration_uniq"
            ),
        ),
        RenameInPlace(
            model_name="calibrationrun",
            old_name="calibration_window_valid",
            item=models.CheckConstraint(
                condition=models.Q(("window_end__gte", models.F("window_start"))),
                name="calibrationrun_window_chk",
            ),
        ),
        RenameInPlace(
            model_name="calibrationrun",
            old_name="calibration_factor_finite",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("factor__isnull", True),
                    models.Q(("factor__gt", 0), ("factor__lt", float("inf"))),
                    _connector="OR",
                ),
                name="calibrationrun_factor_chk",
            ),
        ),
        RenameInPlace(
            model_name="calibrationrun",
            old_name="calibration_status_valid",
            item=models.CheckConstraint(
                condition=models.Q(
                    (
                        "status__in",
                        [
                            "pending",
                            "running",
                            "ready",
                            "insufficient",
                            "rejected",
                            "failed",
                            "superseded",
                        ],
                    )
                ),
                name="calibrationrun_status_chk",
            ),
        ),
        RenameInPlace(
            model_name="calibrationactivation",
            old_name="activation_id_config_unique",
            item=models.UniqueConstraint(
                fields=("id", "configuration"), name="calibrationactivation_id_configuration_uniq"
            ),
        ),
        RenameInPlace(
            model_name="forecastissuance",
            old_name="forecast_issuance_unique",
            item=models.UniqueConstraint(
                fields=("plant", "snapshot", "target_date"),
                name="forecastissuance_plant_snapshot_target_date_uniq",
            ),
        ),
        RenameInPlace(
            model_name="forecastissuance",
            old_name="forecast_issuance_factor_finite",
            item=models.CheckConstraint(
                condition=models.Q(("factor__gt", 0), ("factor__lt", float("inf"))),
                name="forecastissuance_factor_chk",
            ),
        ),
        RenameInPlace(
            model_name="forecastissuance",
            old_name="forecast_issuance_before_daylight",
            item=models.CheckConstraint(
                condition=models.Q(("published_at__lt", models.F("cutoff_at"))),
                name="forecastissuance_published_at_chk",
            ),
        ),
        RenameInPlace(
            model_name="forecastissuance",
            old_name="issuance_baseline_factor_identity",
            item=models.CheckConstraint(
                condition=models.Q(("calibration__isnull", False), ("factor", 1), _connector="OR"),
                name="forecastissuance_calibration_factor_chk",
            ),
        ),
        RenameInPlace(
            model_name="forecastissuance",
            old_name="issuance_plant_day",
            item=models.Index(
                fields=["plant", "target_date"], name="forecastissuance_plant_day_idx"
            ),
        ),
        *schema_objects.operations("plants", "0004_schema_naming_convention"),
    ]
