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
                ("revision", models.PositiveIntegerField(default=1)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Awaiting approval"),
                            ("approved", "Approved"),
                            ("suspended", "Suspended"),
                            ("rejected", "Rejected"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("deleted_at", models.DateTimeField(db_index=True, null=True)),
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
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("region_renamed", "Plant location renamed"),
                            ("location_change_requested", "Location change requested"),
                            ("location_change_approved", "Location change approved"),
                            ("location_change_rejected", "Location change rejected"),
                            (
                                "location_change_rebased",
                                "Location change request moved to the new revision",
                            ),
                            ("binding_configured", "Provider connection configured"),
                            ("connection_validated", "Provider connection validated"),
                            ("region_approved", "Plant approved"),
                            ("region_rejected", "Plant rejected"),
                            ("region_suspended", "Plant suspended"),
                            ("region_trashed", "Plant location moved to trash"),
                            ("region_restored", "Plant location restored"),
                            ("collection_schedule_updated", "Collection schedule updated"),
                            ("historical_reference_imported", "Historical reference imported"),
                            ("reference_capacity_normalized", "Reference capacity normalized"),
                            ("provider_account_created", "Provider account created"),
                            ("provider_secret_rotated", "Provider key rotated"),
                            ("provider_cooldown_cleared", "Provider request pause cleared"),
                        ],
                        max_length=40,
                    ),
                ),
                ("revision", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "provider_account",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="connections.provideraccount",
                    ),
                ),
                (
                    "region",
                    models.ForeignKey(
                        null=True, on_delete=django.db.models.deletion.PROTECT, to="regions.region"
                    ),
                ),
            ],
        ),
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
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
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
        ),
        migrations.AddConstraint(
            model_name="region",
            constraint=models.UniqueConstraint(
                models.F("tenant"),
                models.Func(
                    models.F("latitude"),
                    output_field=models.DecimalField(decimal_places=6, max_digits=10),
                    template="ROUND((%(expressions)s)::numeric, 6)",
                ),
                models.Func(
                    models.F("longitude"),
                    output_field=models.DecimalField(decimal_places=6, max_digits=10),
                    template="ROUND((%(expressions)s)::numeric, 6)",
                ),
                condition=models.Q(("deleted_at__isnull", True)),
                name="one_active_region_per_tenant_location",
            ),
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
                    ("status__in", ["pending", "approved", "suspended", "rejected"])
                ),
                name="region_status",
            ),
        ),
        migrations.AddConstraint(
            model_name="region",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    ("approved_binding__isnull", False),
                    _connector="OR",
                ),
                name="region_status_approved_binding_chk",
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
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "action__in",
                        [
                            "region_renamed",
                            "location_change_requested",
                            "location_change_approved",
                            "location_change_rejected",
                            "location_change_rebased",
                            "binding_configured",
                            "connection_validated",
                            "region_approved",
                            "region_rejected",
                            "region_suspended",
                            "region_trashed",
                            "region_restored",
                            "collection_schedule_updated",
                            "historical_reference_imported",
                            "reference_capacity_normalized",
                            "provider_account_created",
                            "provider_secret_rotated",
                            "provider_cooldown_cleared",
                        ],
                    )
                ),
                name="auditevent_action_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="regionlocationchangerequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "pending")),
                fields=("region",),
                name="one_pending_region_location_request",
            ),
        ),
        migrations.AddConstraint(
            model_name="regionlocationchangerequest",
            constraint=models.CheckConstraint(
                condition=models.Q(("latitude__gte", -90), ("latitude__lte", 90)),
                name="location_request_latitude",
            ),
        ),
        migrations.AddConstraint(
            model_name="regionlocationchangerequest",
            constraint=models.CheckConstraint(
                condition=models.Q(("longitude__gte", -180), ("longitude__lte", 180)),
                name="location_request_longitude",
            ),
        ),
        migrations.AddConstraint(
            model_name="regionlocationchangerequest",
            constraint=models.CheckConstraint(
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
        ),
        migrations.AddConstraint(
            model_name="regionlocationchangerequest",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ["pending", "approved", "rejected"])),
                name="regionlocationchangerequest_status_chk",
            ),
        ),
    ]
