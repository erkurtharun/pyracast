from typing import ClassVar

from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast.db import refuse_values_outside

LAST_ERRORS = ["", "attempt_limit", "invalid_cutoff", "invalid_calibration_data"]

ACTIONS = [
    "submission_saved",
    "configuration_created",
    "plant_renamed",
    "observations_imported",
    "production_changed",
    "epias_connected",
    "epias_disconnected",
    "calibration_requested",
    "calibration_scheduled",
    "calibration_completed",
    "calibration_activated",
    "calibration_automatically_activated",
    "calibration_reset",
    "calibration_automation_enabled",
    "calibration_automation_disabled",
    "calibration_automatic_skipped",
]


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0002_initial"),
        ("plants", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AlterField(
            model_name="calibrationrun",
            name="last_error",
            field=models.CharField(
                blank=True,
                choices=[
                    ("attempt_limit", "Attempt limit reached"),
                    ("invalid_cutoff", "Invalid cutoff"),
                    ("invalid_calibration_data", "Invalid calibration data"),
                ],
                default="",
                max_length=200,
            ),
        ),
        migrations.AlterField(
            model_name="calibrationrun",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("running", "Running"),
                    ("ready", "Ready"),
                    ("insufficient", "Insufficient"),
                    ("rejected", "Rejected"),
                    ("failed", "Failed"),
                    ("superseded", "Superseded"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="plantevent",
            name="action",
            field=models.CharField(
                choices=[
                    ("submission_saved", "Submission saved"),
                    ("configuration_created", "Configuration created"),
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
                ],
                max_length=50,
            ),
        ),
        refuse_values_outside("plants.calibrationrun", "last_error", LAST_ERRORS),
        migrations.AddConstraint(
            model_name="calibrationrun",
            constraint=models.CheckConstraint(
                condition=models.Q(("last_error__in", LAST_ERRORS)),
                name="calibrationrun_last_error_chk",
            ),
        ),
        refuse_values_outside("plants.plantevent", "action", ACTIONS),
        migrations.AddConstraint(
            model_name="plantevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("action__in", ACTIONS)), name="plantevent_action_chk"
            ),
        ),
    ]
