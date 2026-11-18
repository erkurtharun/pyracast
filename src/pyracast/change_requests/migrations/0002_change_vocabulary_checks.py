from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast.db import refuse_values_outside

KINDS = ["", "inverter", "panels", "grid", "other", "multiple"]

BASELINE_SOURCE_KINDS = ["", "manual", "realtime_generation", "settlement_injection"]


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("change_requests", "0001_initial"),
        ("incidents", "0002_incident_kind_choices"),
        ("plants", "0002_plant_event_action_check"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AlterField(
            model_name="incidentchange",
            name="kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("inverter", "Inverter"),
                    ("panels", "Panels"),
                    ("grid", "Grid"),
                    ("other", "Other"),
                    ("multiple", "Multiple"),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="productionchangeitem",
            name="baseline_source_kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("manual", "Manual"),
                    ("realtime_generation", "Real-time generation"),
                    ("settlement_injection", "Settlement injection"),
                ],
                max_length=40,
            ),
        ),
        migrations.AlterField(
            model_name="productionchangeitem",
            name="quality",
            field=models.CharField(
                choices=[
                    ("valid", "Valid"),
                    ("outage", "Outage"),
                    ("curtailed", "Curtailed"),
                    ("invalid", "Invalid"),
                ],
                max_length=20,
            ),
        ),
        refuse_values_outside("change_requests.incidentchange", "kind", KINDS),
        migrations.AddConstraint(
            model_name="incidentchange",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", KINDS)), name="incidentchange_kind_chk"
            ),
        ),
        refuse_values_outside(
            "change_requests.productionchangeitem", "baseline_source_kind", BASELINE_SOURCE_KINDS
        ),
        migrations.AddConstraint(
            model_name="productionchangeitem",
            constraint=models.CheckConstraint(
                condition=models.Q(("baseline_source_kind__in", BASELINE_SOURCE_KINDS)),
                name="productionchangeitem_baseline_source_kind_chk",
            ),
        ),
    ]
