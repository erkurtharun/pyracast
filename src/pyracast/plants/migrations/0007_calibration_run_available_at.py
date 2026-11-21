from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("plants", "0006_submission_updated_at"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="calibrationrun",
            name="available_at",
            field=models.DateTimeField(null=True),
        ),
        # Backfill: when a requeued run started waiting again is unknown, so it waits from the
        # upgrade; every other run waits from its creation.
        migrations.RunSQL(
            "UPDATE plants_calibrationrun SET available_at = CASE "
            "WHEN status = 'pending' AND attempts > 0 THEN now() ELSE created_at END",
            migrations.RunSQL.noop,
        ),
    ]
