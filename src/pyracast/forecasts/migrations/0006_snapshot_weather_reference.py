from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("forecasts", "0005_nightly_history"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="forecastsnapshot",
            name="weather_error",
            field=models.CharField(default="", max_length=64),
        ),
        migrations.AddField(
            model_name="forecastsnapshot",
            name="weather_payload",
            field=models.JSONField(null=True),
        ),
    ]
