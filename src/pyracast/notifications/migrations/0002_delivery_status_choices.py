from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("notifications", "0001_initial"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AlterField(
            model_name="notificationdelivery",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("retry", "Retry"),
                    ("sending", "Sending"),
                    ("sent", "Sent"),
                    ("failed", "Failed"),
                    ("uncertain", "Uncertain"),
                    ("skipped", "Skipped"),
                    ("cancelled", "Cancelled"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
