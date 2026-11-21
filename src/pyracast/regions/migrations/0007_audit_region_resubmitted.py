from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

ACTIONS = [
    ("region_renamed", "Plant location renamed"),
    ("location_change_requested", "Location change requested"),
    ("location_change_approved", "Location change approved"),
    ("location_change_rejected", "Location change rejected"),
    ("location_change_withdrawn", "Location change withdrawn"),
    ("location_change_rebased", "Location change request moved to the new revision"),
    ("binding_configured", "Provider connection configured"),
    ("connection_validated", "Provider connection validated"),
    ("region_approved", "Plant approved"),
    ("region_rejected", "Plant rejected"),
    ("region_suspended", "Plant suspended"),
    ("region_resubmitted", "Plant resubmitted for review"),
    ("region_trashed", "Plant location moved to trash"),
    ("region_restored", "Plant location restored"),
    ("collection_schedule_updated", "Collection schedule updated"),
    ("reminder_schedule_updated", "Email reminders updated"),
    ("historical_reference_imported", "Historical reference imported"),
    ("reference_capacity_normalized", "Reference capacity normalized"),
    ("provider_account_created", "Provider account created"),
    ("provider_secret_rotated", "Provider key rotated"),
    ("provider_cooldown_cleared", "Provider request pause cleared"),
]


class Migration(migrations.Migration):
    dependencies: ClassVar = [("regions", "0006_reminder_schedule_audit")]

    # Adds an audit action; every stored row keeps its value, which the new check still admits.
    operations: ClassVar[list[Operation]] = [
        migrations.RemoveConstraint(
            model_name="auditevent",
            name="auditevent_action_chk",
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="action",
            field=models.CharField(choices=ACTIONS, max_length=40),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("action__in", [value for value, _ in ACTIONS])),
                name="auditevent_action_chk",
            ),
        ),
    ]
