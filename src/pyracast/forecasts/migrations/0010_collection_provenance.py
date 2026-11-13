from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("forecasts", "0009_requestattempt_request_account_day_and_more"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(
            """
            ALTER TABLE forecasts_requestattempt
            ADD CONSTRAINT request_binding_account_fk
            FOREIGN KEY (binding_id, provider_account_id)
            REFERENCES connections_sitebinding (id, provider_account_id)
            DEFERRABLE INITIALLY DEFERRED NOT VALID
            """,
            "ALTER TABLE forecasts_requestattempt DROP CONSTRAINT request_binding_account_fk",
        ),
        migrations.RunSQL(
            """
            ALTER TABLE forecasts_requestattempt
            ADD CONSTRAINT request_job_binding_fk
            FOREIGN KEY (job_id, binding_id)
            REFERENCES forecasts_collectionjob (id, binding_id)
            DEFERRABLE INITIALLY DEFERRED NOT VALID
            """,
            "ALTER TABLE forecasts_requestattempt DROP CONSTRAINT request_job_binding_fk",
        ),
        migrations.RunSQL(
            """
            ALTER TABLE forecasts_collectionjob
            ADD CONSTRAINT collection_import_binding_fk
            FOREIGN KEY (reference_import_id, binding_id)
            REFERENCES forecasts_historicalreferenceimport (id, binding_id)
            DEFERRABLE INITIALLY DEFERRED NOT VALID
            """,
            "ALTER TABLE forecasts_collectionjob DROP CONSTRAINT collection_import_binding_fk",
        ),
    ]
