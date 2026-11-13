from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("connections", "0004_provideraccount_authorization_reference_and_more"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="sitebinding",
            name="settings_verification_reference",
            field=models.CharField(default="", max_length=500),
        ),
    ]
