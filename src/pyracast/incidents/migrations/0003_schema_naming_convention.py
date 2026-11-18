from typing import ClassVar

import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import pyracast.plants.models
from pyracast import schema_objects
from pyracast.db import RenameInPlace


class Migration(migrations.Migration):
    dependencies: ClassVar = [("incidents", "0002_incident_kind_choices")]

    operations: ClassVar[list[Operation]] = [
        migrations.RenameField(
            model_name="incidentrevision", old_name="recorded_at", new_name="created_at"
        ),
        RenameInPlace(
            model_name="plantincident",
            old_name="incident_no_overlap",
            item=django.contrib.postgres.constraints.ExclusionConstraint(
                condition=models.Q(("cancelled_at__isnull", True)),
                expressions=(
                    ("plant", "="),
                    (
                        pyracast.plants.models.TsTzRange(
                            "starts_at",
                            "ends_at",
                            django.contrib.postgres.fields.ranges.RangeBoundary(),
                        ),
                        "&&",
                    ),
                ),
                name="plantincident_plant_interval_excl",
            ),
        ),
        RenameInPlace(
            model_name="plantincident",
            old_name="incident_valid_interval",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("ends_at__isnull", True),
                    ("ends_at__gt", models.F("starts_at")),
                    _connector="OR",
                ),
                name="plantincident_ends_at_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantincident",
            old_name="incident_kind_valid",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("kind__in", ["inverter", "panels", "grid", "other", "multiple"])
                ),
                name="plantincident_kind_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantincident",
            old_name="incident_dc_finite",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("available_dc_mw__isnull", True),
                    models.Q(("available_dc_mw__gte", 0), ("available_dc_mw__lt", float("inf"))),
                    _connector="OR",
                ),
                name="plantincident_available_dc_mw_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantincident",
            old_name="incident_inverter_finite",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("available_inverter_mw__isnull", True),
                    models.Q(
                        ("available_inverter_mw__gte", 0),
                        ("available_inverter_mw__lt", float("inf")),
                    ),
                    _connector="OR",
                ),
                name="plantincident_available_inverter_mw_chk",
            ),
        ),
        RenameInPlace(
            model_name="plantincident",
            old_name="incident_export_finite",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("available_export_mw__isnull", True),
                    models.Q(
                        ("available_export_mw__gte", 0), ("available_export_mw__lt", float("inf"))
                    ),
                    _connector="OR",
                ),
                name="plantincident_available_export_mw_chk",
            ),
        ),
        RenameInPlace(
            model_name="incidentrevision",
            old_name="incident_revision_unique",
            item=models.UniqueConstraint(
                fields=("incident", "revision"), name="incidentrevision_incident_revision_uniq"
            ),
        ),
        *schema_objects.operations("incidents", "0003_schema_naming_convention"),
    ]
