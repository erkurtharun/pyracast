from typing import ClassVar

import django.db.models.expressions
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects
from pyracast.db import RenameInPlace


class Migration(migrations.Migration):
    dependencies: ClassVar = [("forecasts", "0003_lease_import_and_archive_checks")]

    operations: ClassVar[list[Operation]] = [
        migrations.RenameField(
            model_name="collectionjob", old_name="scheduled_slot", new_name="scheduled_for"
        ),
        migrations.RenameField(
            model_name="historicalreferenceimport", old_name="sha256", new_name="content_sha256"
        ),
        migrations.RenameField(
            model_name="historicalreferenceimport", old_name="imported_at", new_name="created_at"
        ),
        RenameInPlace(
            model_name="dispatcherstatus",
            old_name="dispatcher_phase_valid",
            item=models.CheckConstraint(
                condition=models.Q(
                    (
                        "phase__in",
                        ["collection", "notifications", "calibration", "retention", "metering"],
                    )
                ),
                name="dispatcherstatus_phase_chk",
            ),
        ),
        RenameInPlace(
            model_name="historicalreferenceimport",
            old_name="reference_import_binding_identity",
            item=models.UniqueConstraint(
                fields=("id", "binding"), name="historicalreferenceimport_id_binding_uniq"
            ),
        ),
        RenameInPlace(
            model_name="historicalreferenceimport",
            old_name="unique_historical_reference_import",
            item=models.UniqueConstraint(
                fields=("binding", "content_sha256"),
                name="historicalreferenceimport_binding_content_sha256_uniq",
            ),
        ),
        RenameInPlace(
            model_name="collectionjob",
            old_name="collection_job_binding_identity",
            item=models.UniqueConstraint(
                fields=("id", "binding"), name="collectionjob_id_binding_uniq"
            ),
        ),
        RenameInPlace(
            model_name="collectionjob",
            old_name="collection_job_status",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ["pending", "running", "retry", "succeeded", "failed", "stale"])
                ),
                name="collectionjob_status_chk",
            ),
        ),
        RenameInPlace(
            model_name="collectionjob",
            old_name="collectionjob_binding_endpoint_scheduled_slot_uniq",
            item=models.UniqueConstraint(
                condition=models.Q(("endpoint__in", ("forecasts", "estimated_actuals"))),
                fields=("binding", "endpoint", "scheduled_for"),
                name="collectionjob_binding_endpoint_scheduled_for_uniq",
            ),
        ),
        RenameInPlace(
            model_name="collectionjob",
            old_name="unique_import_reference_day",
            item=models.UniqueConstraint(
                fields=("reference_import", "target_date"),
                name="collectionjob_reference_import_target_date_uniq",
            ),
        ),
        RenameInPlace(
            model_name="requestattempt",
            old_name="request_job_requires_binding",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("job__isnull", True), ("binding__isnull", False), _connector="OR"
                ),
                name="requestattempt_job_binding_chk",
            ),
        ),
        RenameInPlace(
            model_name="requestattempt",
            old_name="request_attempt_outcome",
            item=models.CheckConstraint(
                condition=models.Q(("outcome__in", ["uncertain", "succeeded", "failed"])),
                name="requestattempt_outcome_chk",
            ),
        ),
        RenameInPlace(
            model_name="forecastsnapshot",
            old_name="snapshot_binding_identity",
            item=models.UniqueConstraint(
                fields=("id", "binding"), name="forecastsnapshot_id_binding_uniq"
            ),
        ),
        RenameInPlace(
            model_name="forecastsnapshot",
            old_name="snapshot_coverage_bounds",
            item=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("coverage_end__isnull", True), ("coverage_start__isnull", True)),
                    models.Q(
                        ("coverage_end__gt", models.F("coverage_start")),
                        ("coverage_end__isnull", False),
                        ("coverage_start__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="forecastsnapshot_coverage_chk",
            ),
        ),
        RenameInPlace(
            model_name="forecastsnapshot",
            old_name="snapshot_source_kind",
            item=models.CheckConstraint(
                condition=models.Q(("source_kind__in", ["forecast", "estimated_actuals"])),
                name="forecastsnapshot_source_kind_chk",
            ),
        ),
        RenameInPlace(
            model_name="referencecapacitynormalization",
            old_name="normalization_distinct_source",
            item=models.CheckConstraint(
                condition=models.Q(("snapshot", models.F("source_snapshot")), _negated=True),
                name="referencecapacitynormalization_source_snapshot_chk",
            ),
        ),
        RenameInPlace(
            model_name="referencecapacitynormalization",
            old_name="normalization_source_capacity",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("source_capacity_kw__gt", 0), ("source_capacity_kw__lt", float("inf"))
                ),
                name="referencecapacitynormalization_source_capacity_kw_chk",
            ),
        ),
        RenameInPlace(
            model_name="referencecapacitynormalization",
            old_name="normalization_target_capacity",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("target_capacity_kw__gt", 0), ("target_capacity_kw__lt", float("inf"))
                ),
                name="referencecapacitynormalization_target_capacity_kw_chk",
            ),
        ),
        RenameInPlace(
            model_name="referencecapacitynormalization",
            old_name="unique_reference_capacity_normalization",
            item=models.UniqueConstraint(
                fields=("source_snapshot", "target_binding"),
                name="referencecapacitynormalization_source_target_uniq",
            ),
        ),
        RenameInPlace(
            model_name="referencecapacitynormalization",
            old_name="finite_positive_reference_scale",
            item=models.CheckConstraint(
                condition=models.Q(("factor__gt", 0), ("factor__lt", float("inf"))),
                name="referencecapacitynormalization_factor_finite_chk",
            ),
        ),
        RenameInPlace(
            model_name="referencecapacitynormalization",
            old_name="referencecapacitynormalization_factor_chk",
            item=models.CheckConstraint(
                condition=models.Q(
                    (
                        "factor",
                        django.db.models.expressions.CombinedExpression(
                            models.F("target_capacity_kw"), "/", models.F("source_capacity_kw")
                        ),
                    )
                ),
                name="referencecapacitynormalization_target_over_source_chk",
            ),
        ),
        RenameInPlace(
            model_name="collectionjob",
            old_name="collection_dispatch_due",
            item=models.Index(
                condition=models.Q(("status__in", ("pending", "retry", "running"))),
                fields=["available_at", "id"],
                name="collectionjob_due_idx",
            ),
        ),
        RenameInPlace(
            model_name="requestattempt",
            old_name="request_account_day",
            item=models.Index(
                fields=["provider_account", "utc_day"], name="requestattempt_account_day_idx"
            ),
        ),
        RenameInPlace(
            model_name="forecastsnapshot",
            old_name="snapshot_binding_source_age",
            item=models.Index(
                fields=["binding", "source_kind", "-collected_at", "-available_at", "-id"],
                name="forecastsnapshot_latest_idx",
            ),
        ),
        *schema_objects.operations("forecasts", "0004_schema_naming_convention"),
    ]
