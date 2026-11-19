from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("accounts", "0006_expire_invitations_with_the_previous_link"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AlterField(
            model_name="workspaceinvitation",
            name="token_hash",
            field=models.CharField(max_length=64, null=True, unique=True),
        ),
    ]
