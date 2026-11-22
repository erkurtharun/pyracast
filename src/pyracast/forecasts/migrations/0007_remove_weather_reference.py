from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("forecasts", "0006_collection_outcomes_and_ledger_status"),
    ]

    operations: ClassVar[list[Operation]] = [
        *schema_objects.operations("forecasts", "0007_remove_weather_reference"),
        migrations.RemoveConstraint(
            model_name="forecastsnapshot",
            name="forecastsnapshot_weather_error_chk",
        ),
        migrations.RemoveField(
            model_name="forecastsnapshot",
            name="weather_error",
        ),
        migrations.RemoveField(
            model_name="forecastsnapshot",
            name="weather_payload",
        ),
    ]
