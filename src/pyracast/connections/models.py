from django.conf import settings
from django.db import models

from pyracast.connections.identity import canonical_account_identity
from pyracast.regions.models import Region


class ProviderAccount(models.Model):
    provider = models.CharField(max_length=30)
    external_account_id = models.CharField(max_length=200)
    encrypted_api_key = models.CharField(max_length=2048, blank=True, default="")
    daily_quota = models.PositiveIntegerField(default=10)
    enabled = models.BooleanField(default=True)
    credential_revision = models.PositiveIntegerField(default=1)
    request_blocked_until = models.DateTimeField(null=True)
    authentication_failed_revision = models.PositiveIntegerField(null=True)
    authorization_reference = models.CharField(max_length=500, default="")
    authorized_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True)
    authorized_at = models.DateTimeField(null=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                canonical_account_identity("provider"),
                canonical_account_identity("external_account_id"),
                name="canonical_provider_account",
            ),
            models.CheckConstraint(
                condition=models.Q(provider="fixture", encrypted_api_key="")
                | models.Q(encrypted_api_key__startswith="encrypted:v1:"),
                name="provider_account_encrypted_api_key",
            ),
        )


class SiteBinding(models.Model):
    region = models.ForeignKey(Region, on_delete=models.PROTECT, related_name="bindings")
    provider_account = models.ForeignKey(ProviderAccount, on_delete=models.PROTECT)
    region_revision = models.PositiveIntegerField()
    site_id = models.CharField(max_length=200)
    capacity_kw = models.FloatField()
    dc_capacity_kw = models.FloatField(null=True, blank=True)
    power_basis = models.CharField(max_length=2, default="ac")
    capacity_semantics_verified = models.BooleanField(default=False)
    tilt_degrees = models.FloatField(default=0)
    azimuth_degrees = models.FloatField(default=0)
    losses_percent = models.FloatField(default=0)
    installation_date = models.DateField(null=True)
    settings_verification_reference = models.CharField(max_length=500, default="")
    latitude = models.FloatField()
    longitude = models.FloatField()
    timezone = models.CharField(max_length=100)
    validated_at = models.DateTimeField(null=True)
    validated_credential_revision = models.PositiveIntegerField(null=True)
    health = models.CharField(max_length=20, default="unknown")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("id", "region"), name="binding_region_identity"),
            models.UniqueConstraint(
                fields=("id", "provider_account"), name="binding_account_identity"
            ),
            models.CheckConstraint(condition=models.Q(capacity_kw__gt=0), name="binding_capacity"),
            models.CheckConstraint(
                condition=models.Q(capacity_kw__lt=float("inf")), name="binding_capacity_finite"
            ),
            models.CheckConstraint(
                condition=models.Q(dc_capacity_kw__isnull=True)
                | models.Q(dc_capacity_kw__gt=0, dc_capacity_kw__lt=float("inf")),
                name="binding_dc_capacity",
            ),
            models.CheckConstraint(condition=models.Q(tilt_degrees=0), name="horizontal_binding"),
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90, latitude__lte=90), name="binding_latitude"
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__gte=-180, longitude__lte=180),
                name="binding_longitude",
            ),
            models.CheckConstraint(
                condition=models.Q(power_basis__in=("ac", "dc")),
                name="binding_power_basis",
            ),
            models.CheckConstraint(
                condition=models.Q(azimuth_degrees__gte=0, azimuth_degrees__lt=360),
                name="binding_azimuth",
            ),
            models.CheckConstraint(
                condition=models.Q(losses_percent__gte=0, losses_percent__lt=100),
                name="binding_losses",
            ),
            models.CheckConstraint(
                condition=models.Q(health__in=("unknown", "healthy", "error")),
                name="binding_health",
            ),
        )
