from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("forecasts", "0012_forecastsnapshot_binding_and_more"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(
            """
            ALTER TABLE forecasts_forecastsnapshot
            ADD CONSTRAINT snapshot_job_binding_fk
            FOREIGN KEY (job_id, binding_id)
            REFERENCES forecasts_collectionjob (id, binding_id)
            DEFERRABLE INITIALLY DEFERRED NOT VALID
            """,
            "ALTER TABLE forecasts_forecastsnapshot DROP CONSTRAINT snapshot_job_binding_fk",
        ),
        migrations.RunSQL(
            """
            ALTER TABLE forecasts_referencecapacitynormalization
            ADD CONSTRAINT normalization_snapshot_binding_fk
            FOREIGN KEY (snapshot_id, target_binding_id)
            REFERENCES forecasts_forecastsnapshot (id, binding_id)
            DEFERRABLE INITIALLY DEFERRED NOT VALID
            """,
            "ALTER TABLE forecasts_referencecapacitynormalization "
            "DROP CONSTRAINT normalization_snapshot_binding_fk",
        ),
    ]
