from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("plants", "0006_submission_updated_at"),
    ]

    # Every row is kept. A stored submission gets no base, so it awaits approval only while its
    # plant has no configuration, which is how the earlier code activated it.
    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="plant",
            name="parameters_revision",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="plantsubmission",
            name="base_configuration",
            field=models.ForeignKey(
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="+",
                to="plants.plantconfiguration",
            ),
        ),
        *schema_objects.operations("plants", "0007_plant_parameters_revision"),
    ]
