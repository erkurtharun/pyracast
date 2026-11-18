from typing import ClassVar

from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast.db import refuse_rows

REJECTED = "rejected"


def rejected_without_reason(model: type[models.Model]) -> models.Q:
    return models.Q(status=REJECTED, decision_reason="")


def ends_before_start(model: type[models.Model]) -> models.Q:
    return models.Q(ends_at__lte=models.F("starts_at"))


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("change_requests", "0002_change_vocabulary_checks"),
        ("incidents", "0002_incident_kind_choices"),
        ("plants", "0003_lease_check_and_observation_trigger"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        refuse_rows(
            "change_requests.changerequest",
            rejected_without_reason,
            "a rejected request keeps its reason",
        ),
        migrations.AddConstraint(
            model_name="changerequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", REJECTED), _negated=True),
                    models.Q(("decision_reason", ""), _negated=True),
                    _connector="OR",
                ),
                name="changerequest_decision_reason_chk",
            ),
        ),
        refuse_rows(
            "change_requests.incidentchange",
            ends_before_start,
            "an incident proposal with both times ends after it starts",
        ),
        migrations.AddConstraint(
            model_name="incidentchange",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("starts_at__isnull", True),
                    ("ends_at__isnull", True),
                    ("ends_at__gt", models.F("starts_at")),
                    _connector="OR",
                ),
                name="incidentchange_ends_at_chk",
            ),
        ),
    ]
