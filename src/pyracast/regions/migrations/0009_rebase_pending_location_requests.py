from typing import ClassVar, cast

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation


def rebase_pending_requests(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    region = cast(type[models.Model], apps.get_model("regions", "Region"))
    change = cast(type[models.Model], apps.get_model("regions", "RegionLocationChangeRequest"))
    change._default_manager.using(schema_editor.connection.alias).filter(status="pending").exclude(
        base_revision=models.F("region__revision")
    ).update(
        base_revision=models.Subquery(
            region._default_manager.filter(pk=models.OuterRef("region_id")).values("revision")[:1]
        )
    )


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("regions", "0008_remove_region_surface"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunPython(rebase_pending_requests, migrations.RunPython.noop),
    ]
