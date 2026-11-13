from typing import ClassVar

import django.db.models.deletion
from django.contrib.postgres.operations import ValidateConstraint
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("forecasts", "0013_snapshot_lineage"),
    ]

    operations: ClassVar[list[Operation]] = [
        ValidateConstraint("forecastsnapshot", "snapshot_binding_not_null"),
        ValidateConstraint("referencecapacitynormalization", "normalization_distinct_source"),
        ValidateConstraint("referencecapacitynormalization", "normalization_source_capacity"),
        ValidateConstraint("referencecapacitynormalization", "normalization_target_capacity"),
        migrations.RunSQL(
            "ALTER TABLE forecasts_forecastsnapshot VALIDATE CONSTRAINT snapshot_job_binding_fk",
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "ALTER TABLE forecasts_referencecapacitynormalization "
            "VALIDATE CONSTRAINT normalization_snapshot_binding_fk",
            migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="forecastsnapshot",
            name="binding",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="snapshots",
                to="connections.sitebinding",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="forecastsnapshot", name="snapshot_binding_not_null"
        ),
    ]
