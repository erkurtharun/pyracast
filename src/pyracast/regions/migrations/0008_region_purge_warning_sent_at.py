from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("regions", "0007_audit_region_resubmitted"),
    ]

    # Every row is kept. A region already in the trash is marked as warned, so the warning
    # mail never starts with a burst for items trashed before this release.
    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="region",
            name="purge_warning_sent_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunSQL(
            "UPDATE regions_region SET purge_warning_sent_at = now() WHERE deleted_at IS NOT NULL",
            migrations.RunSQL.noop,
        ),
    ]
