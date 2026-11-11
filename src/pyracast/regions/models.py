from django.conf import settings
from django.db import models

from pyracast.accounts.models import Tenant


class Region(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    latitude = models.FloatField()
    longitude = models.FloatField()
    timezone = models.CharField(max_length=100)
    surface = models.CharField(max_length=30, default="horizontal")
    revision = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, default="pending")
    approved_binding = models.ForeignKey(
        "connections.SiteBinding", on_delete=models.PROTECT, null=True, related_name="approvals"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, db_index=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90, latitude__lte=90), name="region_latitude"
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__gte=-180, longitude__lte=180), name="region_longitude"
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=("pending", "approved", "rejected", "suspended")),
                name="region_status",
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
    status = models.CharField(max_length=20, default="pending")
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
                condition=models.Q(status="pending"),
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
                    models.Q(status="pending", reviewed_by__isnull=True, reviewed_at__isnull=True)
                    | models.Q(
                        status__in=("approved", "rejected"),
                        reviewed_by__isnull=False,
                        reviewed_at__isnull=False,
                    )
                ),
                name="location_request_review_state",
            ),
        )


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    region = models.ForeignKey(Region, on_delete=models.PROTECT, null=True)
    provider_account = models.ForeignKey(
        "connections.ProviderAccount", on_delete=models.PROTECT, null=True
    )
    action = models.CharField(max_length=40)
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
        )
