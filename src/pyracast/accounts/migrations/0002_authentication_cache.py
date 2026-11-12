from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [("accounts", "0001_initial")]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="AuthenticationCacheEntry",
            fields=[
                ("cache_key", models.CharField(max_length=255, primary_key=True, serialize=False)),
                ("value", models.JSONField()),
                ("expires", models.DateTimeField(db_index=True, null=True)),
            ],
            options={"db_table": "pyracast_authentication_cache"},
        )
    ]
