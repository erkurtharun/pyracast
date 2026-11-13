from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0005_sitebinding_settings_verification_reference"),
        ("regions", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="auditevent",
            name="provider_account",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="connections.provideraccount",
            ),
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="region",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT, to="regions.region"
            ),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("provider_account__isnull", True), ("region__isnull", False)),
                    models.Q(("provider_account__isnull", False), ("region__isnull", True)),
                    _connector="OR",
                ),
                name="audit_exactly_one_target",
            ),
        ),
    ]
