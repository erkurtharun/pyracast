from typing import ClassVar

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    atomic = False

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("forecasts", "0015_collection_query_indexes"),
    ]

    operations: ClassVar[list[Operation]] = [
        AddIndexConcurrently(
            model_name="forecastsnapshot",
            index=models.Index(
                fields=("binding", "source_kind", "-collected_at", "-available_at", "-id"),
                name="snapshot_binding_source_age",
            ),
        ),
    ]
