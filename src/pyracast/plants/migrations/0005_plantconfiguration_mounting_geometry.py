from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (("plants", "0004_calibrationrun_comparison_activation"),)

    operations = (
        migrations.AddField(
            model_name="plantconfiguration",
            name="mounting_type",
            field=models.CharField(
                choices=[("unknown", "Unknown"), ("fixed", "Fixed"), ("tracking", "Tracking")],
                default="unknown",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="plantconfiguration",
            name="surface_azimuth",
            field=models.FloatField(null=True),
        ),
        migrations.AddField(
            model_name="plantconfiguration",
            name="surface_tilt",
            field=models.FloatField(null=True),
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
    )
