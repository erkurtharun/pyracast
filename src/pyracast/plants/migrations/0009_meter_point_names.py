from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

METER_POINTS = [
    ("net_export", "Energy delivered to the grid"),
    ("ac_generation", "Inverter output"),
]


class Migration(migrations.Migration):
    dependencies: ClassVar = [("plants", "0008_calibration_run_available_at")]

    operations: ClassVar[list[Operation]] = [
        migrations.AlterField(
            model_name=model,
            name="measurement_basis",
            field=models.CharField(choices=METER_POINTS, default="net_export", max_length=20),
        )
        for model in ("plantconfiguration", "plantsubmission")
    ]
