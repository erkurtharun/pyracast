from typing import ClassVar

import django.core.serializers.json
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar = [
        ("incidents", "0001_initial"),
        ("plants", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="ChangeRequest",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("production", "Production"), ("incident", "Incident")],
                        max_length=20,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "pending"),
                            ("approved", "approved"),
                            ("rejected", "rejected"),
                            ("withdrawn", "withdrawn"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("proposer_membership_id", models.PositiveBigIntegerField()),
                ("idempotency_key", models.UUIDField()),
                ("reason", models.CharField(blank=True, max_length=500)),
                ("payload_sha256", models.CharField(max_length=64)),
                ("submitted_at", models.DateTimeField(auto_now_add=True)),
                ("closed_at", models.DateTimeField(null=True)),
                ("decision_reason", models.CharField(blank=True, max_length=500)),
                ("outcome_seen_at", models.DateTimeField(null=True)),
                (
                    "reviewed_context",
                    models.JSONField(
                        default=dict, encoder=django.core.serializers.json.DjangoJSONEncoder
                    ),
                ),
                (
                    "configuration",
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
                    "proposed_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="IncidentChange",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("action", models.CharField(max_length=10)),
                ("expected_revision", models.PositiveIntegerField(null=True)),
                ("kind", models.CharField(blank=True, max_length=20)),
                ("title", models.CharField(blank=True, max_length=200)),
                ("notes", models.CharField(blank=True, max_length=2000)),
                ("starts_at", models.DateTimeField(null=True)),
                ("ends_at", models.DateTimeField(null=True)),
                ("available_dc_mw", models.FloatField(null=True)),
                ("available_inverter_mw", models.FloatField(null=True)),
                ("available_export_mw", models.FloatField(null=True)),
                (
                    "applied_revision",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="incidents.incidentrevision",
                    ),
                ),
                (
                    "incident",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="incidents.plantincident",
                    ),
                ),
                (
                    "request",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="incident_change",
                        to="change_requests.changerequest",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ProductionChangeItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("starts_at", models.DateTimeField()),
                ("ends_at", models.DateTimeField()),
                ("action", models.CharField(max_length=10)),
                ("energy_mwh", models.FloatField()),
                ("quality", models.CharField(max_length=20)),
                ("source", models.CharField(max_length=200)),
                ("baseline_energy_mwh", models.FloatField(null=True)),
                ("baseline_source_kind", models.CharField(blank=True, max_length=40)),
                (
                    "applied_observation",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="plants.productionobservation",
                    ),
                ),
                (
                    "expected_head",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="plants.productionobservation",
                    ),
                ),
                (
                    "request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="production_items",
                        to="change_requests.changerequest",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="changerequest",
            index=models.Index(
                fields=["plant", "status", "submitted_at"], name="change_requ_plant_i_04f7db_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="changerequest",
            constraint=models.UniqueConstraint(
                fields=("proposer_membership_id", "idempotency_key"),
                name="change_request_idempotency_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="changerequest",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", ("production", "incident"))),
                name="change_request_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="changerequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("closed_at__isnull", True),
                        ("decision_reason", ""),
                        ("outcome_seen_at__isnull", True),
                        ("reviewed_by__isnull", True),
                        ("status", "pending"),
                    ),
                    models.Q(
                        ("closed_at__isnull", False),
                        ("outcome_seen_at__isnull", True),
                        ("reviewed_by__isnull", True),
                        ("status", "withdrawn"),
                    ),
                    models.Q(
                        ("closed_at__isnull", False),
                        ("reviewed_by__isnull", False),
                        ("status__in", ("approved", "rejected")),
                    ),
                    _connector="OR",
                ),
                name="change_request_review_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="incidentchange",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("action", "create"),
                        ("expected_revision__isnull", True),
                        ("incident__isnull", True),
                    ),
                    models.Q(
                        ("action__in", ("update", "resolve", "cancel")),
                        ("expected_revision__gte", 1),
                        ("expected_revision__isnull", False),
                        ("incident__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="proposal_incident_target_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="incidentchange",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("action__in", ("resolve", "cancel")),
                    models.Q(
                        ("action__in", ("create", "update")),
                        ("kind__in", ("inverter", "panels", "grid", "other", "multiple")),
                        ("starts_at__isnull", False),
                        models.Q(("title", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="proposal_incident_values_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="incidentchange",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("action", "resolve"), _negated=True),
                    ("ends_at__isnull", False),
                    _connector="OR",
                ),
                name="proposal_incident_end_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="incidentchange",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("available_dc_mw__isnull", True),
                    models.Q(("available_dc_mw__gte", 0), ("available_dc_mw__lt", float("inf"))),
                    _connector="OR",
                ),
                name="proposal_incident_dc_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="incidentchange",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("available_inverter_mw__isnull", True),
                    models.Q(
                        ("available_inverter_mw__gte", 0),
                        ("available_inverter_mw__lt", float("inf")),
                    ),
                    _connector="OR",
                ),
                name="proposal_incident_inverter_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="incidentchange",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("available_export_mw__isnull", True),
                    models.Q(
                        ("available_export_mw__gte", 0), ("available_export_mw__lt", float("inf"))
                    ),
                    _connector="OR",
                ),
                name="proposal_incident_export_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionchangeitem",
            constraint=models.CheckConstraint(
                condition=models.Q(("action__in", ("set", "revert"))),
                name="proposal_production_action_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionchangeitem",
            constraint=models.CheckConstraint(
                condition=models.Q(("energy_mwh__gte", 0), ("energy_mwh__lt", float("inf"))),
                name="proposal_production_energy_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionchangeitem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("baseline_energy_mwh__isnull", True),
                    models.Q(
                        ("baseline_energy_mwh__gte", 0), ("baseline_energy_mwh__lt", float("inf"))
                    ),
                    _connector="OR",
                ),
                name="proposal_baseline_energy_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionchangeitem",
            constraint=models.CheckConstraint(
                condition=models.Q(("ends_at__gt", models.F("starts_at"))),
                name="proposal_production_interval_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionchangeitem",
            constraint=models.CheckConstraint(
                condition=models.Q(("quality__in", ("valid", "outage", "curtailed", "invalid"))),
                name="proposal_production_quality_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionchangeitem",
            constraint=models.UniqueConstraint(
                fields=("request", "starts_at", "ends_at"),
                name="proposal_production_interval_unique",
            ),
        ),
        *schema_objects.operations("change_requests", "0001_initial"),
    ]
