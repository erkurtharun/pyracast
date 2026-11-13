from typing import ClassVar

from django.contrib.postgres.operations import ValidateConstraint
from django.db import migrations


class Migration(migrations.Migration):
    dependencies: ClassVar = [("notifications", "0003_database_invariants")]

    operations: ClassVar = [
        ValidateConstraint("notificationdelivery", "notification_delivery_status_valid"),
    ]
