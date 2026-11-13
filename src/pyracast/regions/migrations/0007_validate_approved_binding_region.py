from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("regions", "0006_approved_binding_region"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(
            "ALTER TABLE regions_region VALIDATE CONSTRAINT approved_binding_region_fk",
            migrations.RunSQL.noop,
        ),
    ]
