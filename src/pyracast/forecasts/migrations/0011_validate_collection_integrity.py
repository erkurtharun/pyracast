from typing import ClassVar

from django.contrib.postgres.operations import ValidateConstraint
from django.db import migrations
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("forecasts", "0010_collection_provenance"),
    ]

    operations: ClassVar[list[Operation]] = [
        ValidateConstraint("collectionjob", "collection_job_status"),
        ValidateConstraint("forecastsnapshot", "snapshot_coverage_bounds"),
        ValidateConstraint("forecastsnapshot", "snapshot_source_kind"),
        ValidateConstraint("requestattempt", "request_job_requires_binding"),
        ValidateConstraint("requestattempt", "request_attempt_outcome"),
        migrations.RunSQL(
            "ALTER TABLE forecasts_requestattempt VALIDATE CONSTRAINT request_binding_account_fk",
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "ALTER TABLE forecasts_requestattempt VALIDATE CONSTRAINT request_job_binding_fk",
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "ALTER TABLE forecasts_collectionjob VALIDATE CONSTRAINT collection_import_binding_fk",
            migrations.RunSQL.noop,
        ),
    ]
