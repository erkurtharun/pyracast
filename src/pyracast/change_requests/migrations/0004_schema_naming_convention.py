from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects
from pyracast.db import RenameInPlace


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("change_requests", "0003_decision_reason_and_incident_interval_checks")
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RenameField(
            model_name="changerequest", old_name="submitted_at", new_name="created_at"
        ),
        RenameInPlace(
            model_name="changerequest",
            old_name="change_request_idempotency_unique",
            item=models.UniqueConstraint(
                fields=("proposer_membership_id", "idempotency_key"),
                name="changerequest_idempotency_key_uniq",
            ),
        ),
        RenameInPlace(
            model_name="changerequest",
            old_name="change_request_kind_valid",
            item=models.CheckConstraint(
                condition=models.Q(("kind__in", ["production", "incident"])),
                name="changerequest_kind_chk",
            ),
        ),
        RenameInPlace(
            model_name="changerequest",
            old_name="change_request_review_state",
            item=models.CheckConstraint(
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
                name="changerequest_review_chk",
            ),
        ),
        RenameInPlace(
            model_name="productionchangeitem",
            old_name="proposal_production_action_valid",
            item=models.CheckConstraint(
                condition=models.Q(("action__in", ["set", "revert"])),
                name="productionchangeitem_action_chk",
            ),
        ),
        RenameInPlace(
            model_name="productionchangeitem",
            old_name="proposal_production_energy_finite",
            item=models.CheckConstraint(
                condition=models.Q(("energy_mwh__gte", 0), ("energy_mwh__lt", float("inf"))),
                name="productionchangeitem_energy_mwh_chk",
            ),
        ),
        RenameInPlace(
            model_name="productionchangeitem",
            old_name="proposal_baseline_energy_finite",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("baseline_energy_mwh__isnull", True),
                    models.Q(
                        ("baseline_energy_mwh__gte", 0), ("baseline_energy_mwh__lt", float("inf"))
                    ),
                    _connector="OR",
                ),
                name="productionchangeitem_baseline_energy_mwh_chk",
            ),
        ),
        RenameInPlace(
            model_name="productionchangeitem",
            old_name="proposal_production_interval_valid",
            item=models.CheckConstraint(
                condition=models.Q(("ends_at__gt", models.F("starts_at"))),
                name="productionchangeitem_ends_at_chk",
            ),
        ),
        RenameInPlace(
            model_name="productionchangeitem",
            old_name="proposal_production_quality_valid",
            item=models.CheckConstraint(
                condition=models.Q(("quality__in", ["valid", "outage", "curtailed", "invalid"])),
                name="productionchangeitem_quality_chk",
            ),
        ),
        RenameInPlace(
            model_name="productionchangeitem",
            old_name="proposal_production_interval_unique",
            item=models.UniqueConstraint(
                fields=("request", "starts_at", "ends_at"),
                name="productionchangeitem_request_starts_at_ends_at_uniq",
            ),
        ),
        RenameInPlace(
            model_name="incidentchange",
            old_name="proposal_incident_target_valid",
            item=models.CheckConstraint(
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
                name="incidentchange_target_chk",
            ),
        ),
        RenameInPlace(
            model_name="incidentchange",
            old_name="proposal_incident_values_valid",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("action__in", ("resolve", "cancel")),
                    models.Q(
                        ("action__in", ("create", "update")),
                        ("kind__in", ["inverter", "panels", "grid", "other", "multiple"]),
                        ("starts_at__isnull", False),
                        models.Q(("title", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="incidentchange_values_chk",
            ),
        ),
        RenameInPlace(
            model_name="incidentchange",
            old_name="proposal_incident_end_required",
            item=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("action", "resolve"), _negated=True),
                    ("ends_at__isnull", False),
                    _connector="OR",
                ),
                name="incidentchange_resolution_chk",
            ),
        ),
        RenameInPlace(
            model_name="incidentchange",
            old_name="proposal_incident_dc_finite",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("available_dc_mw__isnull", True),
                    models.Q(("available_dc_mw__gte", 0), ("available_dc_mw__lt", float("inf"))),
                    _connector="OR",
                ),
                name="incidentchange_available_dc_mw_chk",
            ),
        ),
        RenameInPlace(
            model_name="incidentchange",
            old_name="proposal_incident_inverter_finite",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("available_inverter_mw__isnull", True),
                    models.Q(
                        ("available_inverter_mw__gte", 0),
                        ("available_inverter_mw__lt", float("inf")),
                    ),
                    _connector="OR",
                ),
                name="incidentchange_available_inverter_mw_chk",
            ),
        ),
        RenameInPlace(
            model_name="incidentchange",
            old_name="proposal_incident_export_finite",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("available_export_mw__isnull", True),
                    models.Q(
                        ("available_export_mw__gte", 0), ("available_export_mw__lt", float("inf"))
                    ),
                    _connector="OR",
                ),
                name="incidentchange_available_export_mw_chk",
            ),
        ),
        RenameInPlace(
            model_name="changerequest",
            old_name="change_requ_plant_i_04f7db_idx",
            item=models.Index(
                fields=["plant", "status", "created_at"], name="changerequest_plant_status_idx"
            ),
        ),
        *schema_objects.operations("change_requests", "0004_schema_naming_convention"),
    ]
