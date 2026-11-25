from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("accounts", "0008_invitation_revocation_reason_and_acceptor"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AlterModelOptions(
            name="workspaceadminchange",
            options={
                "verbose_name": "workspace administrator change",
                "verbose_name_plural": "workspace administrator changes",
            },
        ),
        migrations.AlterField(
            model_name="workspaceadminchange",
            name="administrator",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="workspace_admin_assignments",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Workspace administrator",
            ),
        ),
        migrations.AlterField(
            model_name="workspaceadminchange",
            name="previous_administrator",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="previous_workspace_admin_assignments",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Previous workspace administrator",
            ),
        ),
    ]
