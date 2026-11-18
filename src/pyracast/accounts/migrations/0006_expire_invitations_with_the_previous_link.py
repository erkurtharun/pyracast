from typing import ClassVar

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation
from django.utils import timezone


def expire_open_invitations(apps: Apps, _schema_editor: BaseDatabaseSchemaEditor) -> None:
    # Open invitations were mailed with /app/#token=, an entry this release removes.
    invitation = apps.get_model("accounts", "WorkspaceInvitation")
    now = timezone.now()
    invitation.objects.filter(
        accepted_at__isnull=True, revoked_at__isnull=True, expires_at__gt=now
    ).update(expires_at=now)


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("accounts", "0005_schema_naming_convention"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunPython(expire_open_invitations, migrations.RunPython.noop, elidable=True),
    ]
