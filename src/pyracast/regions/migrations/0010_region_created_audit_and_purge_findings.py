from typing import ClassVar

from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

ACTIONS = [
    ("region_created", "Plant submitted"),
    ("region_renamed", "Plant location renamed"),
    ("location_change_requested", "Location change requested"),
    ("location_change_approved", "Location change approved"),
    ("location_change_rejected", "Location change rejected"),
    ("location_change_withdrawn", "Location change withdrawn"),
    ("location_change_rebased", "Location change request moved to the new revision"),
    ("binding_configured", "Provider connection configured"),
    ("connection_validated", "Provider connection validated"),
    ("connection_validation_failed", "Provider connection validation failed"),
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

# A purge that started before this release lists what is left of its region; the rows an
# earlier cycle removed are gone.
FILL_STARTED_PURGES = """
UPDATE regions_region AS region
SET purge_plant_ids = ARRAY(
        SELECT plant.id FROM plants_plant AS plant
        WHERE plant.region_id = region.id ORDER BY plant.id
    ),
    purge_bindings = ARRAY(
        SELECT account.provider || ':' || account.id || ':' || binding.site_id
        FROM connections_sitebinding AS binding
        JOIN connections_provideraccount AS account ON account.id = binding.provider_account_id
        WHERE binding.region_id = region.id ORDER BY binding.id
    )
WHERE region.purge_started_at IS NOT NULL
"""


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0004_schema_naming_convention"),
        ("plants", "0001_initial"),
        ("regions", "0009_audit_connection_validation_failed"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RemoveConstraint(
            model_name="auditevent",
            name="auditevent_action_chk",
        ),
        # Reversing drops the creation events, which the earlier vocabulary refuses.
        migrations.RunSQL(
            migrations.RunSQL.noop,
            "DELETE FROM regions_auditevent WHERE action = 'region_created'",
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
        migrations.AddField(
            model_name="region",
            name="purge_plant_ids",
            field=ArrayField(models.BigIntegerField(), null=True, size=None),
        ),
        migrations.AddField(
            model_name="region",
            name="purge_bindings",
            field=ArrayField(models.TextField(), null=True, size=None),
        ),
        migrations.RunSQL(FILL_STARTED_PURGES, migrations.RunSQL.noop),
        migrations.AddConstraint(
            model_name="region",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    purge_started_at__isnull=True,
                    purge_plant_ids__isnull=True,
                    purge_bindings__isnull=True,
                )
                | models.Q(
                    purge_started_at__isnull=False,
                    purge_plant_ids__isnull=False,
                    purge_bindings__isnull=False,
                ),
                name="region_purge_found_chk",
            ),
        ),
    ]
