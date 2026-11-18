from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects
from pyracast.db import repair_leases


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("metering", "0002_epias_vocabulary_choices"),
        ("plants", "0002_plant_event_action_and_calibration_error_checks"),
    ]

    operations: ClassVar[list[Operation]] = [
        repair_leases("metering.epiascollectionjob", ["running"]),
        migrations.AddConstraint(
            model_name="epiascollectionjob",
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
                name="epiascollectionjob_lease_chk",
            ),
        ),
        *schema_objects.operations("metering", "0003_lease_check_and_snapshot_trigger"),
    ]
