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
        # When a requeued run started waiting again is unknown, so it waits from the upgrade;
        # every other row keeps null, which reads as its creation.
        migrations.RunSQL(
            "UPDATE plants_calibrationrun SET available_at = now() "
            "WHERE status = 'pending' AND attempts > 0",
            migrations.RunSQL.noop,
        ),
    ]
