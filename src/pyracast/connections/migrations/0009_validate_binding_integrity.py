from typing import ClassVar

from django.contrib.postgres.operations import ValidateConstraint
from django.db import migrations
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("connections", "0008_sitebinding_binding_region_identity_and_more"),
    ]

    operations: ClassVar[list[Operation]] = [
        ValidateConstraint("sitebinding", "binding_capacity_finite"),
        ValidateConstraint("sitebinding", "binding_latitude"),
        ValidateConstraint("sitebinding", "binding_longitude"),
        ValidateConstraint("sitebinding", "binding_matching_power_basis"),
        ValidateConstraint("sitebinding", "binding_azimuth"),
        ValidateConstraint("sitebinding", "binding_losses"),
        ValidateConstraint("sitebinding", "binding_health"),
    ]
