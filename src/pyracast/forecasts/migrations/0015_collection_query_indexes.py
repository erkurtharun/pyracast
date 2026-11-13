from typing import ClassVar

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    atomic = False

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("forecasts", "0014_validate_snapshot_lineage"),
    ]

    operations: ClassVar[list[Operation]] = [
        AddIndexConcurrently(
            model_name="collectionjob",
            index=models.Index(
                fields=("available_at", "id"),
                condition=models.Q(status__in=("pending", "retry", "running")),
                name="collection_dispatch_due",
            ),
        ),
    ]
