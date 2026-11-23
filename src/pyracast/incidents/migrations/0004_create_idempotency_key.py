from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("incidents", "0003_schema_naming_convention"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="plantincident",
            name="idempotency_key",
            field=models.UUIDField(null=True),
        ),
        migrations.AddConstraint(
            model_name="plantincident",
            constraint=models.UniqueConstraint(
                fields=("plant", "created_by", "idempotency_key"),
                name="plantincident_idempotency_key_uniq",
            ),
        ),
    ]
