from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("connections", "0005_sitebinding_settings_verification_reference"),
        ("regions", "0004_regionlocationchangerequest"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="sitebinding",
            name="dc_capacity_kw",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="sitebinding",
            name="azimuth_degrees",
            field=models.FloatField(default=0),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("dc_capacity_kw__isnull", True),
                    models.Q(("dc_capacity_kw__gt", 0), ("dc_capacity_kw__lt", float("inf"))),
                    _connector="OR",
                ),
                name="binding_dc_capacity",
            ),
        ),
    ]
