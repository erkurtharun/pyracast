from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects
from pyracast.db import RenameInPlace


class Migration(migrations.Migration):
    dependencies: ClassVar = [("metering", "0003_lease_check_and_snapshot_trigger")]

    operations: ClassVar[list[Operation]] = [
        migrations.RenameField(
            model_name="epiascollectionjob", old_name="target_day", new_name="target_date"
        ),
        migrations.RenameField(
            model_name="epiascollectionjob", old_name="next_attempt_at", new_name="available_at"
        ),
        migrations.RenameField(
            model_name="epiassnapshot", old_name="target_day", new_name="target_date"
        ),
        RenameInPlace(
            model_name="epiasconnection",
            old_name="epias_connection_plant_identity",
            item=models.UniqueConstraint(
                fields=("id", "plant"), name="epiasconnection_id_plant_uniq"
            ),
        ),
        RenameInPlace(
            model_name="epiasconnection",
            old_name="epias_revision_positive",
            item=models.CheckConstraint(
                condition=models.Q(("revision__gte", 1)), name="epiasconnection_revision_chk"
            ),
        ),
        RenameInPlace(
            model_name="epiascollectionjob",
            old_name="epias_unique_collection_slot",
            item=models.UniqueConstraint(
                fields=(
                    "connection",
                    "connection_revision",
                    "configuration",
                    "source_kind",
                    "target_date",
                    "scheduled_for",
                ),
                name="epiascollectionjob_slot_uniq",
            ),
        ),
        RenameInPlace(
            model_name="epiascollectionjob",
            old_name="epias_job_lineage_unique",
            item=models.UniqueConstraint(
                fields=("id", "connection", "configuration"),
                name="epiascollectionjob_id_connection_configuration_uniq",
            ),
        ),
        RenameInPlace(
            model_name="epiascollectionjob",
            old_name="epias_job_period_valid",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("period_end_day__isnull", True),
                    ("period_end_day__gte", models.F("target_date")),
                    _connector="OR",
                ),
                name="epiascollectionjob_period_end_day_chk",
            ),
        ),
        RenameInPlace(
            model_name="epiascollectionjob",
            old_name="epias_job_revision_positive",
            item=models.CheckConstraint(
                condition=models.Q(("connection_revision__gte", 1)),
                name="epiascollectionjob_connection_revision_chk",
            ),
        ),
        RenameInPlace(
            model_name="epiascollectionjob",
            old_name="epias_job_source_valid",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("source_kind__in", ["realtime_generation", "settlement_injection"])
                ),
                name="epiascollectionjob_source_kind_chk",
            ),
        ),
        RenameInPlace(
            model_name="epiascollectionjob",
            old_name="epias_job_status_valid",
            item=models.CheckConstraint(
                condition=models.Q(
                    (
                        "status__in",
                        [
                            "pending",
                            "running",
                            "succeeded",
                            "failed",
                            "superseded",
                            "awaiting_publication",
                        ],
                    )
                ),
                name="epiascollectionjob_status_chk",
            ),
        ),
        RenameInPlace(
            model_name="epiassnapshot",
            old_name="epias_unique_job_acquisition",
            item=models.UniqueConstraint(
                fields=("job", "attempt", "target_date"),
                name="epiassnapshot_job_attempt_target_date_uniq",
            ),
        ),
        RenameInPlace(
            model_name="epiassnapshot",
            old_name="epias_acquisition_attempt_positive",
            item=models.CheckConstraint(
                condition=models.Q(("attempt__gte", 1)), name="epiassnapshot_attempt_chk"
            ),
        ),
        RenameInPlace(
            model_name="epiassnapshot",
            old_name="epias_acquisition_source_valid",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("source_kind__in", ["realtime_generation", "settlement_injection"])
                ),
                name="epiassnapshot_source_kind_chk",
            ),
        ),
        RenameInPlace(
            model_name="epiascollectionjob",
            old_name="metering_ep_status_11e228_idx",
            item=models.Index(fields=["status", "available_at"], name="epiascollectionjob_due_idx"),
        ),
        RenameInPlace(
            model_name="epiassnapshot",
            old_name="metering_ep_connect_df32e3_idx",
            item=models.Index(
                fields=["connection", "target_date", "available_at"],
                name="epiassnapshot_connection_idx",
            ),
        ),
        *schema_objects.operations("metering", "0004_schema_naming_convention"),
    ]
