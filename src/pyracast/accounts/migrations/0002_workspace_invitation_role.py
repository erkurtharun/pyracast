from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation
from django.db.models.functions import Lower, Trim


class Migration(migrations.Migration):
    dependencies: ClassVar = [("accounts", "0001_initial")]

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
        migrations.AlterConstraint(
            model_name="tenant",
            name="unique_workspace_name",
            constraint=models.UniqueConstraint(
                Lower(Trim("name")),
                name="unique_workspace_name",
                violation_error_message="This workspace name is already in use.",
            ),
        ),
    ]
