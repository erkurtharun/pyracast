from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast.db import repair_leases


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0003_provider_account_and_binding_checks"),
        ("forecasts", "0003_lease_import_and_archive_checks"),
        ("notifications", "0002_delivery_vocabulary_checks"),
    ]

    operations: ClassVar[list[Operation]] = [
        repair_leases("notifications.notificationdelivery", ["sending"], kept=["uncertain"]),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("lease_expires_at__isnull", False),
                        ("lease_token__isnull", False),
                        ("status__in", ["sending", "uncertain"]),
                    ),
                    models.Q(
                        ("lease_expires_at__isnull", True),
                        ("lease_token__isnull", True),
                        models.Q(("status__in", ["sending"]), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="notificationdelivery_lease_chk",
            ),
        ),
    ]
