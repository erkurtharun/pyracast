from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [("plants", "0009_technical_details_names")]

    operations: ClassVar[list[Operation]] = [
        migrations.RemoveField(model_name="forecastissuance", name="native_intervals"),
    ]
