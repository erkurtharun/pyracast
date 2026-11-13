from typing import ClassVar

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("metering", "0002_remove_epiascollectionjob_epias_unique_collection_slot_and_more"),
    ]

    operations: ClassVar = [
        migrations.AddField(
            model_name="epiascollectionjob",
            name="retry_until",
            field=models.DateTimeField(null=True),
        ),
    ]
