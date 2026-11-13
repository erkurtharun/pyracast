import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (
        ("regions", "0003_region_deleted_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    )

    operations = (
        migrations.CreateModel(
            name="RegionLocationChangeRequest",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("base_revision", models.PositiveIntegerField()),
                ("latitude", models.FloatField()),
                ("longitude", models.FloatField()),
                ("timezone", models.CharField(max_length=100)),
                ("reason", models.CharField(blank=True, max_length=1000)),
                ("status", models.CharField(default="pending", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("reviewed_at", models.DateTimeField(null=True)),
                (
                    "region",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="regions.region"
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="region_location_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reviewed_region_location_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("status", "pending")),
                        fields=("region",),
                        name="one_pending_region_location_request",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("latitude__gte", -90), ("latitude__lte", 90)),
                        name="location_request_latitude",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("longitude__gte", -180), ("longitude__lte", 180)),
                        name="location_request_longitude",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("reviewed_at__isnull", True),
                                ("reviewed_by__isnull", True),
                                ("status", "pending"),
                            ),
                            models.Q(
                                ("reviewed_at__isnull", False),
                                ("reviewed_by__isnull", False),
                                ("status__in", ("approved", "rejected")),
                            ),
                            _connector="OR",
                        ),
                        name="location_request_review_state",
                    ),
                ],
            },
        ),
    )
