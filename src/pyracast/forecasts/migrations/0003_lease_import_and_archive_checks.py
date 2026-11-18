from typing import ClassVar

from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects
from pyracast.db import fire_deferred_checks, refuse_lease_breaches, refuse_rows

IMPORT = "historical_import"


def release_stale_leases(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Main's claim marked a running job whose lease had expired stale and kept that lease;
    the job is finished, so the lease is dropped. No other lease shape is changed."""
    jobs = apps.get_model("forecasts", "CollectionJob")._default_manager
    jobs.using(schema_editor.connection.alias).filter(
        status="stale", lease_token__isnull=False, lease_expires_at__isnull=False
    ).update(lease_token=None, lease_expires_at=None)
    fire_deferred_checks(schema_editor)


def import_endpoint_mismatch(model: type[models.Model]) -> models.Q:
    return models.Q(endpoint=IMPORT, reference_import__isnull=True) | (
        ~models.Q(endpoint=IMPORT) & models.Q(reference_import__isnull=False)
    )


def unfinished_import(model: type[models.Model]) -> models.Q:
    return models.Q(reference_import__isnull=False) & ~models.Q(status="succeeded")


def refuse_factor_mismatch(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    # Compared in Python: PostgreSQL raises instead of answering when a stored quotient leaves
    # float8, while Python yields inf or 0.0, which no stored factor equals.
    rows = apps.get_model("forecasts", "ReferenceCapacityNormalization")._default_manager
    found = sorted(
        key
        for key, factor, source, target in rows.using(schema_editor.connection.alias)
        .values_list("pk", "factor", "source_capacity_kw", "target_capacity_kw")
        .iterator()
        if factor != target / source
    )
    if found:
        raise ValueError(
            f"forecasts_referencecapacitynormalization holds {len(found)} row(s) that break the "
            f"rule 'the factor is the target capacity divided by the source capacity': ids "
            f"{found}. Correct these rows and migrate again; nothing was changed."
        )


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0003_provider_account_and_binding_checks"),
        ("forecasts", "0002_collection_endpoint_and_request_purpose_checks"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RemoveConstraint(
            model_name="collectionjob",
            name="unique_collection_slot",
        ),
        migrations.RunPython(release_stale_leases, migrations.RunPython.noop),
        refuse_lease_breaches("forecasts.collectionjob", ["running"]),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("lease_expires_at__isnull", False),
                        ("lease_token__isnull", False),
                        ("status__in", ["running"]),
                    ),
                    models.Q(
                        ("lease_expires_at__isnull", True),
                        ("lease_token__isnull", True),
                        models.Q(("status__in", ["running"]), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="collectionjob_lease_chk",
            ),
        ),
        refuse_rows(
            "forecasts.collectionjob",
            import_endpoint_mismatch,
            "a reference import uses the historical import endpoint and nothing else does",
        ),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("endpoint", IMPORT), ("reference_import__isnull", False)),
                    models.Q(
                        models.Q(("endpoint", IMPORT), _negated=True),
                        ("reference_import__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="collectionjob_reference_import_chk",
            ),
        ),
        refuse_rows(
            "forecasts.collectionjob",
            unfinished_import,
            "an imported reference day is stored as a succeeded import",
        ),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("reference_import__isnull", True), ("status", "succeeded"), _connector="OR"
                ),
                name="collectionjob_import_status_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.UniqueConstraint(
                condition=models.Q(("endpoint__in", ("forecasts", "estimated_actuals"))),
                fields=("binding", "endpoint", "scheduled_slot"),
                name="collectionjob_binding_endpoint_scheduled_slot_uniq",
            ),
        ),
        migrations.RunPython(refuse_factor_mismatch, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="referencecapacitynormalization",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("factor", models.F("target_capacity_kw") / models.F("source_capacity_kw"))
                ),
                name="referencecapacitynormalization_factor_chk",
            ),
        ),
        *schema_objects.operations("forecasts", "0003_lease_import_and_archive_checks"),
    ]
