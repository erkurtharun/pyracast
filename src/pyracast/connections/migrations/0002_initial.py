from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("connections", "0001_initial"),
        ("regions", "0001_initial"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="sitebinding",
            name="region",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="bindings",
                to="regions.region",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("capacity_kw__gt", 0)), name="binding_capacity"
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("tilt_degrees", 0)), name="horizontal_binding"
            ),
        ),
    ]
