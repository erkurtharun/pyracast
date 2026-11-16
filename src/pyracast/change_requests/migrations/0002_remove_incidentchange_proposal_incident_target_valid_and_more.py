from typing import ClassVar

import django.core.serializers.json
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("change_requests", "0001_initial"),
        ("incidents", "0003_validate_lineage"),
    ]

    operations: ClassVar = [
        migrations.RunSQL(
            (
                "ALTER TABLE change_requests_changerequest ADD CONSTRAINT "
                "proposal_configuration_owner_fk FOREIGN KEY (configuration_id, plant_id) "
                "REFERENCES plants_plantconfiguration (id, plant_id) "
                "DEFERRABLE INITIALLY DEFERRED"
            ),
            (
                "ALTER TABLE change_requests_changerequest DROP CONSTRAINT "
                "proposal_configuration_owner_fk"
            ),
        ),
        migrations.RemoveConstraint(
            model_name="incidentchange",
            name="proposal_incident_target_valid",
        ),
        migrations.AddField(
            model_name="changerequest",
            name="reviewed_context",
            field=models.JSONField(
                default=dict, encoder=django.core.serializers.json.DjangoJSONEncoder
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
    ]
