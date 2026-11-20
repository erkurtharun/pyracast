from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("plants", "0005_calibration_preparation_due_at"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="plantsubmission",
            name="updated_at",
            field=models.DateTimeField(null=True),
        ),
        # Backfill: the last change of a stored submission is unknown; it starts at creation.
        migrations.RunSQL(
            "UPDATE plants_plantsubmission SET updated_at = created_at",
            migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="plantsubmission",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
    ]
