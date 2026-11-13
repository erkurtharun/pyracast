from typing import ClassVar

from django.contrib.postgres.operations import ValidateConstraint
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False
    dependencies: ClassVar = [("plants", "0012_lineage_foreign_keys")]

    operations: ClassVar = [
        ValidateConstraint("calibrationrun", "calibration_window_valid"),
        ValidateConstraint("calibrationrun", "calibration_factor_finite"),
        ValidateConstraint("calibrationrun", "calibration_status_valid"),
        ValidateConstraint("plantconfiguration", "plant_config_revision_positive"),
        ValidateConstraint("plantconfiguration", "plant_measurement_basis_valid"),
        ValidateConstraint("plantsubmission", "submission_dc_capacity_finite"),
        ValidateConstraint("plantsubmission", "submission_inverter_finite"),
        ValidateConstraint("plantsubmission", "submission_export_finite"),
        ValidateConstraint("plantsubmission", "submission_measurement_basis"),
        ValidateConstraint("plantsubmission", "submission_mounting_supported"),
        ValidateConstraint("plantsubmission", "submission_tilt_valid"),
        ValidateConstraint("plantsubmission", "submission_azimuth_valid"),
        ValidateConstraint("plantsubmission", "submission_angles_require_fixed"),
        ValidateConstraint("productionobservation", "plant_observation_quality_valid"),
        migrations.RunSQL(
            "ALTER TABLE plants_plant VALIDATE CONSTRAINT plant_current_config_owner_fk",
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            (
                "ALTER TABLE plants_productionobservation VALIDATE CONSTRAINT "
                "observation_config_owner_fk"
            ),
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "ALTER TABLE plants_calibrationrun VALIDATE CONSTRAINT calibration_config_owner_fk",
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "ALTER TABLE plants_calibrationrun VALIDATE CONSTRAINT calibration_config_binding_fk",
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            (
                "ALTER TABLE plants_calibrationactivation VALIDATE CONSTRAINT "
                "activation_config_owner_fk"
            ),
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            (
                "ALTER TABLE plants_calibrationactivation VALIDATE CONSTRAINT "
                "activation_calibration_config_fk"
            ),
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            (
                "ALTER TABLE plants_calibrationrun VALIDATE CONSTRAINT "
                "calibration_comparison_config_fk"
            ),
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "ALTER TABLE plants_plantforecast VALIDATE CONSTRAINT forecast_calibration_config_fk",
            migrations.RunSQL.noop,
        ),
    ]
