from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

ACTIONS = [
    ("region_created", "Plant sent for approval"),
    ("region_renamed", "Plant renamed"),
    ("location_change_requested", "Location change requested"),
    ("location_change_approved", "Location change approved"),
    ("location_change_rejected", "Location change rejected"),
    ("location_change_withdrawn", "Location change withdrawn"),
    ("location_change_rebased", "Location change request moved to the new revision"),
    ("binding_configured", "Provider connection configured"),
    ("connection_validated", "Provider connection validated"),
    ("connection_validation_failed", "Provider connection validation failed"),
    ("region_approved", "Plant approved"),
    ("region_rejected", "Plant not approved"),
    ("region_suspended", "Plant suspended"),
    ("region_resubmitted", "Plant sent for approval again"),
    ("region_trashed", "Plant moved to recycle bin"),
    ("region_restored", "Plant restored"),
    ("collection_schedule_updated", "Forecast update times changed"),
    ("reminder_schedule_updated", "Forecast email updated"),
    ("historical_reference_imported", "Historical reference imported"),
    ("reference_capacity_normalized", "Reference capacity normalized"),
    ("provider_account_created", "Provider account created"),
    ("provider_secret_rotated", "Provider key rotated"),
    ("provider_cooldown_cleared", "Provider request pause cleared"),
]


class Migration(migrations.Migration):
    dependencies: ClassVar = [("regions", "0010_region_created_audit_and_purge_findings")]

    operations: ClassVar[list[Operation]] = [
        migrations.AlterField(
            model_name="auditevent",
            name="action",
            field=models.CharField(choices=ACTIONS, max_length=40),
        ),
        migrations.AlterField(
            model_name="region",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "In review"),
                    ("approved", "Approved"),
                    ("suspended", "Suspended"),
                    ("rejected", "Not approved"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
