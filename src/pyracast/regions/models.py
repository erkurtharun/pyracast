from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy

from pyracast.accounts.models import Tenant
from pyracast.db import choices_check


class RegionStatus(models.TextChoices):
    PENDING = "pending", gettext_lazy("Awaiting approval")
    APPROVED = "approved", gettext_lazy("Approved")
    SUSPENDED = "suspended", gettext_lazy("Suspended")
    REJECTED = "rejected", gettext_lazy("Rejected")


class LocationChangeStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class AuditAction(models.TextChoices):
    REGION_RENAMED = "region_renamed", gettext_lazy("Plant location renamed")
    LOCATION_CHANGE_REQUESTED = (
        "location_change_requested",
        gettext_lazy("Location change requested"),
    )
    LOCATION_CHANGE_APPROVED = "location_change_approved", gettext_lazy("Location change approved")
    LOCATION_CHANGE_REJECTED = "location_change_rejected", gettext_lazy("Location change rejected")
    LOCATION_CHANGE_REBASED = (
        "location_change_rebased",
        gettext_lazy("Location change request moved to the new revision"),
    )
    BINDING_CONFIGURED = "binding_configured", gettext_lazy("Provider connection configured")
    CONNECTION_VALIDATED = "connection_validated", gettext_lazy("Provider connection validated")
    REGION_APPROVED = "region_approved", gettext_lazy("Plant approved")
    REGION_REJECTED = "region_rejected", gettext_lazy("Plant rejected")
    REGION_SUSPENDED = "region_suspended", gettext_lazy("Plant suspended")
    REGION_TRASHED = "region_trashed", gettext_lazy("Plant location moved to trash")
    REGION_RESTORED = "region_restored", gettext_lazy("Plant location restored")
    COLLECTION_SCHEDULE_UPDATED = (
        "collection_schedule_updated",
        gettext_lazy("Collection schedule updated"),
    )
    HISTORICAL_REFERENCE_IMPORTED = (
        "historical_reference_imported",
        gettext_lazy("Historical reference imported"),
    )
    REFERENCE_CAPACITY_NORMALIZED = (
        "reference_capacity_normalized",
        gettext_lazy("Reference capacity normalized"),
    )
    PROVIDER_ACCOUNT_CREATED = "provider_account_created", gettext_lazy("Provider account created")
    PROVIDER_SECRET_ROTATED = "provider_secret_rotated", gettext_lazy("Provider key rotated")
    PROVIDER_COOLDOWN_CLEARED = (
        "provider_cooldown_cleared",
        gettext_lazy("Provider request pause cleared"),
    )


REGION_TRANSITIONS: dict[str, AuditAction] = {
    RegionStatus.REJECTED: AuditAction.REGION_REJECTED,
    RegionStatus.SUSPENDED: AuditAction.REGION_SUSPENDED,
}


def location_coordinate(field: str) -> models.Func:
    return models.Func(
        models.F(field),
        template="ROUND((%(expressions)s)::numeric, 6)",
        output_field=models.DecimalField(max_digits=10, decimal_places=6),
    )


class Region(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    latitude = models.FloatField()
    longitude = models.FloatField()
    timezone = models.CharField(max_length=100)
    revision = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=RegionStatus, default=RegionStatus.PENDING)
    approved_binding = models.ForeignKey(
        "connections.SiteBinding", on_delete=models.PROTECT, null=True, related_name="approvals"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, db_index=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                models.F("tenant"),
                location_coordinate("latitude"),
                location_coordinate("longitude"),
                condition=models.Q(deleted_at__isnull=True),
                name="one_active_region_per_tenant_location",
            ),
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90, latitude__lte=90), name="region_latitude"
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__gte=-180, longitude__lte=180), name="region_longitude"
            ),
            choices_check("status", RegionStatus, name="region_status"),
            models.CheckConstraint(
                condition=~models.Q(status=RegionStatus.APPROVED)
                | models.Q(approved_binding__isnull=False),
                name="region_status_approved_binding_chk",
            ),
        )


class RegionLocationChangeRequest(models.Model):
    region = models.ForeignKey(Region, on_delete=models.CASCADE)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="region_location_requests"
    )
    base_revision = models.PositiveIntegerField()
    latitude = models.FloatField()
    longitude = models.FloatField()
    timezone = models.CharField(max_length=100)
    reason = models.CharField(max_length=1000, blank=True)
    status = models.CharField(
        max_length=20, choices=LocationChangeStatus, default=LocationChangeStatus.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="reviewed_region_location_requests",
    )
    reviewed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("region",),
                condition=models.Q(status=LocationChangeStatus.PENDING),
                name="one_pending_region_location_request",
            ),
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90, latitude__lte=90),
                name="location_request_latitude",
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__gte=-180, longitude__lte=180),
                name="location_request_longitude",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status=LocationChangeStatus.PENDING,
                        reviewed_by__isnull=True,
                        reviewed_at__isnull=True,
                    )
                    | models.Q(
                        status__in=(LocationChangeStatus.APPROVED, LocationChangeStatus.REJECTED),
                        reviewed_by__isnull=False,
                        reviewed_at__isnull=False,
                    )
                ),
                name="location_request_review_state",
            ),
            choices_check("status", LocationChangeStatus),
        )


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    region = models.ForeignKey(Region, on_delete=models.PROTECT, null=True)
    provider_account = models.ForeignKey(
        "connections.ProviderAccount", on_delete=models.PROTECT, null=True
    )
    action = models.CharField(max_length=40, choices=AuditAction)
    revision = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=(
                    models.Q(region__isnull=False, provider_account__isnull=True)
                    | models.Q(region__isnull=True, provider_account__isnull=False)
                ),
                name="audit_exactly_one_target",
            ),
            choices_check("action", AuditAction),
        )
