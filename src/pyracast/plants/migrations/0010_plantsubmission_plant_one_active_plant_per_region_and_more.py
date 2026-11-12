import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def require_single_active_plant(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    plant = apps.get_model("plants", "Plant")
    conflicts = list(
        plant.objects.using(schema_editor.connection.alias)
        .filter(deleted_at__isnull=True)
        .values("region_id")
        .annotate(plants=models.Count("id"))
        .filter(plants__gt=1)
        .values_list("region_id", flat=True)[:20]
    )
    if conflicts:
        raise RuntimeError(
            f"Multiple active plants in regions {conflicts}; resolve explicitly before migration. "
            "No existing records have been removed."
        )


class Migration(migrations.Migration):
    dependencies = (
        ("plants", "0009_calibration_automation"),
        ("regions", "0004_regionlocationchangerequest"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    )

    operations = (
        migrations.RunPython(require_single_active_plant, migrations.RunPython.noop),
        migrations.CreateModel(
            name="PlantSubmission",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("dc_capacity_mw", models.FloatField()),
                ("inverter_capacity_mw", models.FloatField(null=True)),
                ("export_limit_mw", models.FloatField(null=True)),
                (
                    "measurement_basis",
                    models.CharField(
                        choices=(("net_export", "Net export"), ("ac_generation", "AC generation")),
                        default="net_export",
                        max_length=20,
                    ),
                ),
                (
                    "mounting_type",
                    models.CharField(
                        choices=(
                            ("unknown", "Unknown"),
                            ("fixed", "Fixed"),
                            ("tracking", "Tracking"),
                        ),
                        default="unknown",
                        max_length=10,
                    ),
                ),
                ("surface_tilt", models.FloatField(null=True)),
                ("surface_azimuth", models.FloatField(null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name="plant",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True)),
                fields=("region",),
                name="one_active_plant_per_region",
            ),
        ),
        migrations.AddField(
            model_name="plantsubmission",
            name="plant",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="submission",
                to="plants.plant",
            ),
        ),
        migrations.AddField(
            model_name="plantsubmission",
            name="submitted_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
            ),
        ),
    )
