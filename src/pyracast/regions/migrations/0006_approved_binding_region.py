from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("connections", "0009_validate_binding_integrity"),
        ("regions", "0005_region_one_active_region_per_tenant_location"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(
            """
            ALTER TABLE regions_region
            ADD CONSTRAINT approved_binding_region_fk
            FOREIGN KEY (approved_binding_id, id)
            REFERENCES connections_sitebinding (id, region_id)
            DEFERRABLE INITIALLY DEFERRED NOT VALID
            """,
            "ALTER TABLE regions_region DROP CONSTRAINT approved_binding_region_fk",
        ),
    ]
