from typing import ClassVar

from django.conf import settings
from django.contrib.postgres.operations import AddConstraintNotValid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0008_sitebinding_binding_region_identity_and_more"),
        ("plants", "0010_plantsubmission_plant_one_active_plant_per_region_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.AddConstraint(
            model_name="calibrationactivation",
            constraint=models.UniqueConstraint(
                fields=("id", "configuration"), name="activation_id_config_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="calibrationrun",
            constraint=models.UniqueConstraint(
                fields=("id", "configuration"), name="calibration_id_config_unique"
            ),
        ),
        AddConstraintNotValid(
            model_name="calibrationrun",
            constraint=models.CheckConstraint(
                condition=models.Q(("window_end__gte", models.F("window_start"))),
                name="calibration_window_valid",
            ),
        ),
        AddConstraintNotValid(
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
        AddConstraintNotValid(
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
        AddConstraintNotValid(
            model_name="plantconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(("revision__gte", 1)), name="plant_config_revision_positive"
            ),
        ),
        AddConstraintNotValid(
            model_name="plantconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(("measurement_basis__in", ("net_export", "ac_generation"))),
                name="plant_measurement_basis_valid",
            ),
        ),
        AddConstraintNotValid(
            model_name="plantsubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(("dc_capacity_mw__gt", 0), ("dc_capacity_mw__lt", float("inf"))),
                name="submission_dc_capacity_finite",
            ),
        ),
        AddConstraintNotValid(
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
        AddConstraintNotValid(
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
        AddConstraintNotValid(
            model_name="plantsubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(("measurement_basis__in", ("net_export", "ac_generation"))),
                name="submission_measurement_basis",
            ),
        ),
        AddConstraintNotValid(
            model_name="plantsubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(("mounting_type__in", ("unknown", "fixed", "tracking"))),
                name="submission_mounting_supported",
            ),
        ),
        AddConstraintNotValid(
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
        AddConstraintNotValid(
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
        AddConstraintNotValid(
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
        AddConstraintNotValid(
            model_name="productionobservation",
            constraint=models.CheckConstraint(
                condition=models.Q(("quality__in", ("valid", "outage", "curtailed", "invalid"))),
                name="plant_observation_quality_valid",
            ),
        ),
    ]
