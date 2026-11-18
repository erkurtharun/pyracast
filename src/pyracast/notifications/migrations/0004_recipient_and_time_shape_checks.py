from typing import ClassVar

import django.db.models.lookups
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import pyracast.db
from pyracast.db import json_shape, refuse_rows

TEXT_ITEMS = 'strict $[*] ? (@.type() != "string")'
MEMBERSHIP_ITEMS = (
    'strict $.* ? (@.type() != "array"'
    ' || exists(@[*] ? (@.type() != "number" || @ < 1 || @.floor() != @)))'
)
TIME_ITEMS = (
    'strict $[*] ? (@.type() != "string" || !(@ like_regex "^([01][0-9]|2[0-3]):[0-5][0-9]$"))'
)
SHAPES = {
    "recipients": ("array", TEXT_ITEMS, "recipients are a list of addresses"),
    "recipient_memberships": (
        "object",
        MEMBERSHIP_ITEMS,
        "recipient memberships map each address to membership ids",
    ),
    "revoked_recipients": ("array", TEXT_ITEMS, "revoked recipients are a list of addresses"),
    "times": ("array", TIME_ITEMS, "reminder times are a list of HH:MM times"),
}


def shape_check(model_name: str, field: str) -> list[Operation]:
    kind, breach, rule = SHAPES[field]
    return [
        refuse_rows(
            f"notifications.{model_name}", lambda _: ~json_shape(field, kind, breach), rule
        ),
        migrations.AddConstraint(
            model_name=model_name,
            constraint=models.CheckConstraint(
                condition=models.Q(
                    django.db.models.lookups.Exact(pyracast.db.JsonType(field), kind),
                    models.Q(
                        pyracast.db.JsonPathExists(field, models.Value(breach)), _negated=True
                    ),
                ),
                name=f"{model_name}_{field}_chk",
            ),
        ),
    ]


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0003_provider_account_and_binding_checks"),
        ("forecasts", "0003_lease_import_and_archive_checks"),
        ("notifications", "0003_delivery_lease_check"),
        ("regions", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        *shape_check("reminderschedule", "recipients"),
        *shape_check("reminderschedule", "recipient_memberships"),
        *shape_check("reminderschedule", "times"),
        *shape_check("notificationdelivery", "recipients"),
        *shape_check("notificationdelivery", "recipient_memberships"),
        *shape_check("notificationdelivery", "revoked_recipients"),
    ]
