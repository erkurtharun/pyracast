from typing import ClassVar

from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects
from pyracast.db import refuse_rows, repair_leases


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
            "plants.productionobservation",
            revision_of_another_interval,
            "a revision supersedes the same plant and interval",
        ),
        *schema_objects.operations("plants", "0003_lease_check_and_observation_trigger"),
    ]
