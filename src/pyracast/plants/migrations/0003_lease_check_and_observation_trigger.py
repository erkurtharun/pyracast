from typing import ClassVar

from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects
from pyracast.db import repair_leases


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
        *schema_objects.operations("plants", "0003_lease_check_and_observation_trigger"),
    ]
