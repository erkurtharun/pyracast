import re
import sys
from typing import ClassVar

import django.db.models.lookups
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation

import pyracast.db

CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def keep_valid_times(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """A schedule whose times break the new check keeps only its distinct valid HH:MM times in
    order; a value that is no list becomes an empty one. The repaired rows are counted."""
    schedules = apps.get_model("forecasts", "CollectionSchedule")._default_manager.using(
        schema_editor.connection.alias
    )
    repaired = 0
    for pk, times in schedules.values_list("pk", "times").iterator():
        valid = (
            [clock for clock in times if isinstance(clock, str) and CLOCK.fullmatch(clock)]
            if isinstance(times, list)
            else []
        )
        if not isinstance(times, list) or len(valid) != len(times):
            schedules.filter(pk=pk).update(times=sorted(set(valid)))
            repaired += 1
    if repaired:
        sys.stdout.write(
            f"\n  forecasts: {repaired} collection schedule(s) held times outside HH:MM; "
            "only their valid times were kept."
        )


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("forecasts", "0004_schema_naming_convention"),
        ("regions", "0003_schema_naming_convention"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunPython(keep_valid_times, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="collectionschedule",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    django.db.models.lookups.Exact(pyracast.db.JsonType("times"), "array"),
                    models.Q(
                        pyracast.db.JsonPathExists(
                            "times",
                            models.Value(
                                'strict $[*] ? (@.type() != "string"'
                                ' || !(@ like_regex "^([01][0-9]|2[0-3]):[0-5][0-9]$"))'
                            ),
                        ),
                        _negated=True,
                    ),
                ),
                name="collectionschedule_times_chk",
            ),
        ),
    ]
