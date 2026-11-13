from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def require_distinct_active_locations(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    region = apps.get_model("regions", "Region")
    conflicts = (
        region.objects.using(schema_editor.connection.alias)
        .filter(deleted_at__isnull=True)
        .annotate(
            lat=models.Func(models.F("latitude"), template="ROUND((%(expressions)s)::numeric, 6)"),
            lon=models.Func(models.F("longitude"), template="ROUND((%(expressions)s)::numeric, 6)"),
        )
        .values("tenant_id", "lat", "lon")
        .annotate(regions=models.Count("id"))
        .filter(regions__gt=1)
    )
    if conflicts.exists():
        raise RuntimeError(
            "Duplicate active tenant locations at six decimal precision; resolve explicitly "
            "before migration. No existing records have been removed."
        )


class Migration(migrations.Migration):
    dependencies = (
        ("accounts", "0001_initial"),
        ("connections", "0006_reference_dc_capacity"),
        ("regions", "0004_regionlocationchangerequest"),
    )

    operations = (
        migrations.RunPython(require_distinct_active_locations, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="region",
            constraint=models.UniqueConstraint(
                models.F("tenant"),
                models.Func(
                    models.F("latitude"),
                    output_field=models.DecimalField(decimal_places=6, max_digits=10),
                    template="ROUND((%(expressions)s)::numeric, 6)",
                ),
                models.Func(
                    models.F("longitude"),
                    output_field=models.DecimalField(decimal_places=6, max_digits=10),
                    template="ROUND((%(expressions)s)::numeric, 6)",
                ),
                condition=models.Q(("deleted_at__isnull", True)),
                name="one_active_region_per_tenant_location",
            ),
        ),
    )
