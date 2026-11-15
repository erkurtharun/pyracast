from typing import ClassVar

import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import pyracast.plants.models


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("plants", "0016_validate_configuration_region"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.RemoveConstraint(
            model_name="productionobservation",
            name="plant_observation_unique",
        ),
        migrations.RemoveConstraint(
            model_name="productionobservation",
            name="plant_observation_no_overlap",
        ),
        migrations.AddField(
            model_name="productionobservation",
            name="is_current",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="productionobservation",
            name="is_reverted",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="productionobservation",
            name="supersedes",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="replacement",
                to="plants.productionobservation",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionobservation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_current", True)),
                fields=("plant", "starts_at", "ends_at"),
                name="plant_observation_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionobservation",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
                condition=models.Q(("is_current", True), ("is_reverted", False)),
                expressions=(
                    ("plant", "="),
                    (
                        pyracast.plants.models.TsTzRange(
                            "starts_at",
                            "ends_at",
                            django.contrib.postgres.fields.ranges.RangeBoundary(),
                        ),
                        "&&",
                    ),
                ),
                name="plant_observation_no_overlap",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionobservation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("is_reverted", False), ("supersedes__isnull", False), _connector="OR"
                ),
                name="plant_observation_revert_has_source",
            ),
        ),
    ]
