from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("incidents", "0001_initial"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AlterField(
            model_name="plantincident",
            name="kind",
            field=models.CharField(
                choices=[
                    ("inverter", "Inverter"),
                    ("panels", "Panels"),
                    ("grid", "Grid"),
                    ("other", "Other"),
                    ("multiple", "Multiple"),
                ],
                max_length=20,
            ),
        ),
    ]
