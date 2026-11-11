import uuid

from django.conf import settings
from django.db import models

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.regions.models import Region


class CollectionSchedule(models.Model):
    region = models.OneToOneField(Region, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=True)
    times = models.JSONField(default=list)
    effective_from = models.DateTimeField()


class HistoricalReferenceImport(models.Model):
    binding = models.ForeignKey(SiteBinding, on_delete=models.PROTECT)
    sha256 = models.CharField(max_length=64)
    source_name = models.CharField(max_length=255)
    source_settings = models.JSONField()
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    imported_at = models.DateTimeField()
    interval_count = models.PositiveIntegerField()

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("binding", "sha256"), name="unique_historical_reference_import"
            ),
        )


class CollectionJob(models.Model):
    binding = models.ForeignKey(SiteBinding, on_delete=models.PROTECT)
    reference_import = models.ForeignKey(
        HistoricalReferenceImport, on_delete=models.PROTECT, null=True
    )
    endpoint = models.CharField(max_length=30, default="forecasts")
    scheduled_slot = models.DateTimeField()
    target_date = models.DateField()
    status = models.CharField(max_length=20, default="pending")
    available_at = models.DateTimeField()
    retry_until = models.DateTimeField(null=True)
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=40, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("binding", "endpoint", "scheduled_slot"),
                condition=models.Q(reference_import__isnull=True),
                name="unique_collection_slot",
            ),
            models.UniqueConstraint(
                fields=("reference_import", "target_date"), name="unique_import_reference_day"
            ),
        )


class RequestAttempt(models.Model):
    provider_account = models.ForeignKey(ProviderAccount, on_delete=models.PROTECT)
    binding = models.ForeignKey(SiteBinding, on_delete=models.PROTECT, null=True)
    job = models.ForeignKey(CollectionJob, on_delete=models.PROTECT, null=True)
    utc_day = models.DateField()
    created_at = models.DateTimeField()
    purpose = models.CharField(max_length=20)
    outcome = models.CharField(max_length=20, default="uncertain")
    correlation_id = models.UUIDField(default=uuid.uuid4, unique=True)


class ForecastSnapshot(models.Model):
    job = models.OneToOneField(CollectionJob, on_delete=models.PROTECT)
    raw_payload = models.JSONField()
    normalized_intervals = models.JSONField()
    collected_at = models.DateTimeField()
    issued_at = models.DateTimeField(null=True)
    source_kind = models.CharField(max_length=20, default="unknown")
    coverage_start = models.DateTimeField(null=True, db_index=True)
    coverage_end = models.DateTimeField(null=True, db_index=True)
    available_at = models.DateTimeField(null=True)
    weather_payload = models.JSONField(null=True)
    weather_error = models.CharField(max_length=64, default="")


class ForecastResult(models.Model):
    snapshot = models.OneToOneField(ForecastSnapshot, on_delete=models.PROTECT)
    payload = models.JSONField()
    published_at = models.DateTimeField()
