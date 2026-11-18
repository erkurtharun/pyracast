from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast.db import RenameInPlace


class Migration(migrations.Migration):
    dependencies: ClassVar = [("regions", "0002_audit_event_details")]

    operations: ClassVar[list[Operation]] = [
        RenameInPlace(
            model_name="region",
            old_name="one_active_region_per_tenant_location",
            item=models.UniqueConstraint(
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
                name="region_tenant_location_uniq",
            ),
        ),
        RenameInPlace(
            model_name="region",
            old_name="region_latitude",
            item=models.CheckConstraint(
                condition=models.Q(("latitude__gte", -90), ("latitude__lte", 90)),
                name="region_latitude_chk",
            ),
        ),
        RenameInPlace(
            model_name="region",
            old_name="region_longitude",
            item=models.CheckConstraint(
                condition=models.Q(("longitude__gte", -180), ("longitude__lte", 180)),
                name="region_longitude_chk",
            ),
        ),
        RenameInPlace(
            model_name="region",
            old_name="region_status",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ["pending", "approved", "suspended", "rejected"])
                ),
                name="region_status_chk",
            ),
        ),
        RenameInPlace(
            model_name="regionlocationchangerequest",
            old_name="one_pending_region_location_request",
            item=models.UniqueConstraint(
                condition=models.Q(("status", "pending")),
                fields=("region",),
                name="regionlocationchangerequest_region_uniq",
            ),
        ),
        RenameInPlace(
            model_name="regionlocationchangerequest",
            old_name="location_request_latitude",
            item=models.CheckConstraint(
                condition=models.Q(("latitude__gte", -90), ("latitude__lte", 90)),
                name="regionlocationchangerequest_latitude_chk",
            ),
        ),
        RenameInPlace(
            model_name="regionlocationchangerequest",
            old_name="location_request_longitude",
            item=models.CheckConstraint(
                condition=models.Q(("longitude__gte", -180), ("longitude__lte", 180)),
                name="regionlocationchangerequest_longitude_chk",
            ),
        ),
        RenameInPlace(
            model_name="regionlocationchangerequest",
            old_name="location_request_review_state",
            item=models.CheckConstraint(
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
                name="regionlocationchangerequest_review_chk",
            ),
        ),
        RenameInPlace(
            model_name="auditevent",
            old_name="audit_exactly_one_target",
            item=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("provider_account__isnull", True), ("region__isnull", False)),
                    models.Q(("provider_account__isnull", False), ("region__isnull", True)),
                    _connector="OR",
                ),
                name="auditevent_target_chk",
            ),
        ),
    ]
