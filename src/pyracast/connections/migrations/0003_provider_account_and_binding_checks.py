from typing import ClassVar

from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast.db import refuse_rows

MAX_DAILY_QUOTA = 10


def quota_outside(model: type[models.Model]) -> models.Q:
    return models.Q(daily_quota__lt=1) | models.Q(daily_quota__gt=MAX_DAILY_QUOTA)


def revision_below_one(model: type[models.Model]) -> models.Q:
    return models.Q(credential_revision__lt=1)


def failure_outside_revisions(model: type[models.Model]) -> models.Q:
    return models.Q(authentication_failed_revision__lt=1) | models.Q(
        authentication_failed_revision__gt=models.F("credential_revision")
    )


def invalid_validation(model: type[models.Model]) -> models.Q:
    return (
        models.Q(validated_at__isnull=True, validated_credential_revision__isnull=False)
        | models.Q(validated_at__isnull=False, validated_credential_revision__isnull=True)
        | models.Q(validated_credential_revision__lt=1)
    )


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
        refuse_rows("connections.provideraccount", quota_outside, "a daily quota between 1 and 10"),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.CheckConstraint(
                condition=models.Q(("daily_quota__gte", 1), ("daily_quota__lte", MAX_DAILY_QUOTA)),
                name="provideraccount_daily_quota_chk",
            ),
        ),
        refuse_rows(
            "connections.provideraccount", revision_below_one, "a credential revision from 1"
        ),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.CheckConstraint(
                condition=models.Q(("credential_revision__gte", 1)),
                name="provideraccount_credential_revision_chk",
            ),
        ),
        refuse_rows(
            "connections.provideraccount",
            failure_outside_revisions,
            "an authentication failure names a credential revision of the account",
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
        refuse_rows(
            "connections.sitebinding",
            invalid_validation,
            "a validation stores its moment with a credential revision from 1",
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
                        ("validated_credential_revision__gte", 1),
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
