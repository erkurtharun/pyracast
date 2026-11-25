from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

METER_POINTS = [
    ("net_export", "Energy delivered to the grid"),
    ("ac_generation", "Inverter output"),
]
EVENT_ACTIONS = [
    ("submission_saved", "Submission saved"),
    ("configuration_created", "Technical details revision created"),
    ("plant_renamed", "Plant renamed"),
    ("observations_imported", "Observations imported"),
    ("production_changed", "Production changed"),
    ("epias_connected", "EPIAS connected"),
    ("epias_disconnected", "EPIAS disconnected"),
    ("calibration_requested", "Calibration requested"),
    ("calibration_scheduled", "Calibration scheduled"),
    ("calibration_completed", "Calibration completed"),
    ("calibration_activated", "Calibration activated"),
    ("calibration_automatically_activated", "Calibration automatically activated"),
    ("calibration_reset", "Calibration reset"),
    ("calibration_automation_enabled", "Automatic calibration enabled"),
    ("calibration_automation_disabled", "Automatic calibration disabled"),
    ("calibration_automatic_skipped", "Automatic calibration skipped"),
]


class Migration(migrations.Migration):
    dependencies: ClassVar = [("plants", "0008_calibration_run_available_at")]

    operations: ClassVar[list[Operation]] = [
        migrations.AlterField(
            model_name="plantsubmission",
            name="measurement_basis",
            field=models.CharField(choices=METER_POINTS, default="net_export", max_length=20),
        ),
        migrations.AlterModelOptions(
            name="plantconfiguration",
            options={
                "verbose_name": "technical details",
                "verbose_name_plural": "technical details",
            },
        ),
        migrations.AlterField(
            model_name="plantconfiguration",
            name="revision",
            field=models.PositiveIntegerField(verbose_name="Technical details revision"),
        ),
        migrations.AlterField(
            model_name="plantconfiguration",
            name="dc_capacity_mw",
            field=models.FloatField(verbose_name="Panel capacity (MWp)"),
        ),
        migrations.AlterField(
            model_name="plantconfiguration",
            name="inverter_capacity_mw",
            field=models.FloatField(null=True, verbose_name="Inverter capacity (MW AC)"),
        ),
        migrations.AlterField(
            model_name="plantconfiguration",
            name="export_limit_mw",
            field=models.FloatField(null=True, verbose_name="Grid export limit (MW)"),
        ),
        migrations.AlterField(
            model_name="plantconfiguration",
            name="mounting_type",
            field=models.CharField(
                choices=[("unknown", "Unknown"), ("fixed", "Fixed"), ("tracking", "Tracking")],
                default="unknown",
                max_length=10,
                verbose_name="Mounting",
            ),
        ),
        migrations.AlterField(
            model_name="plantconfiguration",
            name="surface_tilt_degrees",
            field=models.FloatField(null=True, verbose_name="Panel tilt (°)"),
        ),
        migrations.AlterField(
            model_name="plantconfiguration",
            name="surface_azimuth_degrees",
            field=models.FloatField(null=True, verbose_name="Panel azimuth (°)"),
        ),
        migrations.AlterField(
            model_name="plantconfiguration",
            name="measurement_basis",
            field=models.CharField(
                choices=METER_POINTS,
                default="net_export",
                max_length=20,
                verbose_name="Meter point",
            ),
        ),
        migrations.AlterField(
            model_name="plantevent",
            name="action",
            field=models.CharField(choices=EVENT_ACTIONS, max_length=50),
        ),
    ]
