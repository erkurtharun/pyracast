from typing import ClassVar

import django.db.models.lookups
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import pyracast.db


class Migration(migrations.Migration):
    dependencies: ClassVar = [("regions", "0001_initial")]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="auditevent",
            name="details",
            field=models.JSONField(default=dict),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    django.db.models.lookups.Exact(pyracast.db.JsonType("details"), "object")
                ),
                name="auditevent_details_chk",
            ),
        ),
    ]
