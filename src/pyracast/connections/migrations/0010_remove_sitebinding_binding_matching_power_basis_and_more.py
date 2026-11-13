from typing import ClassVar, cast

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation


def require_matching_capacity_basis(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    binding = cast(type[models.Model], apps.get_model("connections", "SiteBinding"))
    conflicts = list(
        binding._default_manager.using(schema_editor.connection.alias)
        .exclude(capacity_basis=models.F("power_basis"))
        .values_list("pk", flat=True)[:10]
    )
    if conflicts:
        raise RuntimeError(
            "Cannot remove SiteBinding.capacity_basis: mismatched power and capacity bases exist "
            f"(sample IDs: {conflicts}). Resolve these values explicitly before migration."
        )


def restore_capacity_basis(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    binding = cast(type[models.Model], apps.get_model("connections", "SiteBinding"))
    binding._default_manager.using(schema_editor.connection.alias).update(
        capacity_basis=models.F("power_basis")
    )


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("connections", "0009_validate_binding_integrity"),
        ("regions", "0008_remove_region_surface"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunPython(require_matching_capacity_basis, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="sitebinding",
            name="binding_matching_power_basis",
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("power_basis__in", ("ac", "dc"))), name="binding_power_basis"
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, restore_capacity_basis),
        migrations.RemoveField(
            model_name="sitebinding",
            name="capacity_basis",
        ),
    ]
