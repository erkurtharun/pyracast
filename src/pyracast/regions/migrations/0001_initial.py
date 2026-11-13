from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar = [
        ("accounts", "0001_initial"),
        ("connections", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="Region",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=200)),
                ("latitude", models.FloatField()),
                ("longitude", models.FloatField()),
                ("timezone", models.CharField(max_length=100)),
                ("surface", models.CharField(default="horizontal", max_length=30)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("status", models.CharField(default="pending", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "approved_binding",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approvals",
                        to="connections.sitebinding",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="accounts.tenant"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="AuditEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("action", models.CharField(max_length=40)),
                ("revision", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "region",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="regions.region"
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="region",
            constraint=models.CheckConstraint(
                condition=models.Q(("latitude__gte", -90), ("latitude__lte", 90)),
                name="region_latitude",
            ),
        ),
        migrations.AddConstraint(
            model_name="region",
            constraint=models.CheckConstraint(
                condition=models.Q(("longitude__gte", -180), ("longitude__lte", 180)),
                name="region_longitude",
            ),
        ),
        migrations.AddConstraint(
            model_name="region",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ("pending", "approved", "rejected", "suspended"))
                ),
                name="region_status",
            ),
        ),
    ]
