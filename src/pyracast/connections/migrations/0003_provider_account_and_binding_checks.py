from typing import ClassVar

from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation

from pyracast.db import fire_deferred_checks, refuse_rows

MAX_DAILY_QUOTA = 10


def repair_accounts_and_bindings(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Clamp a quota into 1..10, raise a zero credential revision to 1, drop an authentication
    failure recorded for a revision the account never had, and clear a validation stored
    without its moment or its credential revision (the binding is validated again)."""
    alias = schema_editor.connection.alias
    accounts = apps.get_model("connections", "ProviderAccount")._default_manager.using(alias)
    accounts.filter(daily_quota__lt=1).update(daily_quota=1)
    accounts.filter(daily_quota__gt=MAX_DAILY_QUOTA).update(daily_quota=MAX_DAILY_QUOTA)
    accounts.filter(credential_revision__lt=1).update(credential_revision=1)
    accounts.filter(
        models.Q(authentication_failed_revision__lt=1)
        | models.Q(authentication_failed_revision__gt=models.F("credential_revision"))
    ).update(authentication_failed_revision=None)
    bindings = apps.get_model("connections", "SiteBinding")._default_manager.using(alias)
    bindings.filter(
        models.Q(validated_at__isnull=True, validated_credential_revision__isnull=False)
        | models.Q(validated_at__isnull=False, validated_credential_revision__isnull=True)
    ).update(validated_at=None, validated_credential_revision=None)
    fire_deferred_checks(schema_editor)


def shared_region_revision(model: type[models.Model]) -> models.Q:
    return models.Q(
        models.Exists(
            model._default_manager.filter(
                region=models.OuterRef("region"), region_revision=models.OuterRef("region_revision")
            ).exclude(pk=models.OuterRef("pk"))
        )
    )


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0002_initial"),
        ("regions", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunPython(repair_accounts_and_bindings, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.CheckConstraint(
                condition=models.Q(("daily_quota__gte", 1), ("daily_quota__lte", MAX_DAILY_QUOTA)),
                name="provideraccount_daily_quota_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.CheckConstraint(
                condition=models.Q(("credential_revision__gte", 1)),
                name="provideraccount_credential_revision_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("authentication_failed_revision__isnull", True),
                    models.Q(
                        ("authentication_failed_revision__gte", 1),
                        ("authentication_failed_revision__lte", models.F("credential_revision")),
                    ),
                    _connector="OR",
                ),
                name="provideraccount_authentication_failed_revision_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("validated_at__isnull", True),
                        ("validated_credential_revision__isnull", True),
                    ),
                    models.Q(
                        ("validated_at__isnull", False),
                        ("validated_credential_revision__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="sitebinding_validated_chk",
            ),
        ),
        refuse_rows(
            "connections.sitebinding", shared_region_revision, "one binding per region revision"
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.UniqueConstraint(
                fields=("region", "region_revision"),
                name="sitebinding_region_region_revision_uniq",
            ),
        ),
    ]
