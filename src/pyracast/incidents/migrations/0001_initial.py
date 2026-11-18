from typing import ClassVar

import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import pyracast.plants.models
from pyracast import schema_objects


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar = [
        ("plants", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="PlantIncident",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("kind", models.CharField(max_length=20)),
                ("title", models.CharField(max_length=200)),
                ("notes", models.CharField(blank=True, max_length=2000)),
                ("starts_at", models.DateTimeField()),
                ("ends_at", models.DateTimeField(null=True)),
                ("available_dc_mw", models.FloatField(null=True)),
                ("available_inverter_mw", models.FloatField(null=True)),
                ("available_export_mw", models.FloatField(null=True)),
                ("cancelled_at", models.DateTimeField(null=True)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "declared_configuration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="plants.plantconfiguration"
                    ),
                ),
                (
                    "plant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="plants.plant"
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="IncidentRevision",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("revision", models.PositiveIntegerField()),
                ("snapshot", models.JSONField()),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "incident",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="revisions",
                        to="incidents.plantincident",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="plantincident",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
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
                name="incident_no_overlap",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantincident",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("ends_at__isnull", True),
                    ("ends_at__gt", models.F("starts_at")),
                    _connector="OR",
                ),
                name="incident_valid_interval",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantincident",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("kind__in", ("inverter", "panels", "grid", "other", "multiple"))
                ),
                name="incident_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantincident",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("available_dc_mw__isnull", True),
                    models.Q(("available_dc_mw__gte", 0), ("available_dc_mw__lt", float("inf"))),
                    _connector="OR",
                ),
                name="incident_dc_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantincident",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("available_inverter_mw__isnull", True),
                    models.Q(
                        ("available_inverter_mw__gte", 0),
                        ("available_inverter_mw__lt", float("inf")),
                    ),
                    _connector="OR",
                ),
                name="incident_inverter_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="plantincident",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("available_export_mw__isnull", True),
                    models.Q(
                        ("available_export_mw__gte", 0), ("available_export_mw__lt", float("inf"))
                    ),
                    _connector="OR",
                ),
                name="incident_export_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="incidentrevision",
            constraint=models.UniqueConstraint(
                fields=("incident", "revision"), name="incident_revision_unique"
            ),
        ),
        *schema_objects.operations("incidents", "0001_initial"),
    ]
