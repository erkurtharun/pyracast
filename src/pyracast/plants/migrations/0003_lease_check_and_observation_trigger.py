from typing import ClassVar

from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects
from pyracast.db import refuse_rows, refuse_values_outside, repair_leases

OPEN = ("pending", "running")
ASSESSMENTS = [
    ("insufficient", "Insufficient"),
    ("provisional", "Provisional"),
    ("evaluated", "Evaluated"),
]


def finished_without_completion(model: type[models.Model]) -> models.Q:
    return ~models.Q(status__in=OPEN) & models.Q(completed_at__isnull=True)


def partial_automation(model: type[models.Model]) -> models.Q:
    enabled = models.Q(
        automatic_calibration_enabled=True,
        automatic_calibration_enabled_by__isnull=False,
        next_calibration_check_at__isnull=False,
    )
    disabled = models.Q(
        automatic_calibration_enabled=False,
        automatic_calibration_enabled_by__isnull=True,
        next_calibration_check_at__isnull=True,
    )
    return ~(enabled | disabled)


def revision_of_another_interval(model: type[models.Model]) -> models.Q:
    same = model._default_manager.filter(
        pk=models.OuterRef("supersedes"),
        plant=models.OuterRef("plant"),
        starts_at=models.OuterRef("starts_at"),
        ends_at=models.OuterRef("ends_at"),
    )
    return models.Q(supersedes__isnull=False) & ~models.Q(models.Exists(same))


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0003_provider_account_and_binding_checks"),
        ("plants", "0002_plant_event_action_and_calibration_error_checks"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        repair_leases("plants.calibrationrun", ["running"]),
        migrations.AddConstraint(
            model_name="calibrationrun",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("lease_expires_at__isnull", False),
                        ("lease_token__isnull", False),
                        ("status__in", ["running"]),
                    ),
                    models.Q(
                        ("lease_expires_at__isnull", True),
                        ("lease_token__isnull", True),
                        models.Q(("status__in", ["running"]), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="calibrationrun_lease_chk",
            ),
        ),
        refuse_rows(
            "plants.calibrationrun",
            finished_without_completion,
            "a terminal run carries its completion time",
        ),
        migrations.AddConstraint(
            model_name="calibrationrun",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", OPEN), ("completed_at__isnull", False), _connector="OR"
                ),
                name="calibrationrun_completed_at_chk",
            ),
        ),
        refuse_values_outside(
            "plants.calibrationrun", "assessment", ["", *(value for value, _ in ASSESSMENTS)]
        ),
        migrations.AlterField(
            model_name="calibrationrun",
            name="assessment",
            field=models.CharField(blank=True, choices=ASSESSMENTS, default="", max_length=200),
        ),
        migrations.AddConstraint(
            model_name="calibrationrun",
            constraint=models.CheckConstraint(
                condition=models.Q(("assessment__in", ["", *(value for value, _ in ASSESSMENTS)])),
                name="calibrationrun_assessment_chk",
            ),
        ),
        refuse_rows(
            "plants.plant",
            partial_automation,
            "automation is enabled with its user and next check or holds neither",
        ),
        migrations.AddConstraint(
            model_name="plant",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("automatic_calibration_enabled", True),
                        ("automatic_calibration_enabled_by__isnull", False),
                        ("next_calibration_check_at__isnull", False),
                    ),
                    models.Q(
                        ("automatic_calibration_enabled", False),
                        ("automatic_calibration_enabled_by__isnull", True),
                        ("next_calibration_check_at__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="plant_automation_chk",
            ),
        ),
        refuse_rows(
            "plants.productionobservation",
            revision_of_another_interval,
            "a revision supersedes the same plant and interval",
        ),
        *schema_objects.operations("plants", "0003_lease_check_and_observation_trigger"),
    ]
