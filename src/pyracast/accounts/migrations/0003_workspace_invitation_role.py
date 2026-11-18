from typing import ClassVar

from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("accounts", "0002_workspace_names_need_not_be_unique"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="workspaceinvitation",
            name="role",
            field=models.CharField(
                choices=[("admin", "Administrator"), ("viewer", "Viewer")],
                default="viewer",
                max_length=6,
            ),
        ),
        migrations.AddConstraint(
            model_name="workspaceinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(("role__in", ["admin", "viewer"])),
                name="workspaceinvitation_role_chk",
            ),
        ),
    ]
