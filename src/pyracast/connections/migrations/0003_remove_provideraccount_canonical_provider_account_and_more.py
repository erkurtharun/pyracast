from typing import ClassVar

import django.db.models.functions.text
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("connections", "0002_initial"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RemoveConstraint(
            model_name="provideraccount",
            name="canonical_provider_account",
        ),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower(
                    django.db.models.functions.text.Trim("provider")
                ),
                django.db.models.functions.text.Lower(
                    django.db.models.functions.text.Trim("external_account_id")
                ),
                name="canonical_provider_account",
            ),
        ),
    ]
