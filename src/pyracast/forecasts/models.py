import uuid

from django.conf import settings
from django.db import models

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.regions.models import Region


class DispatcherStatus(models.Model):
    class Phase(models.TextChoices):
        COLLECTION = "collection"
        NOTIFICATIONS = "notifications"
        CALIBRATION = "calibration"
        RETENTION = "retention"
        METERING = "metering"

    phase = models.CharField(max_length=20, primary_key=True, choices=Phase)
    attempted_at = models.DateTimeField()
    succeeded_at = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=100, blank=True, default="")
    duration_ms = models.PositiveIntegerField(null=True)
    published = models.PositiveIntegerField(null=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=models.Q(
                    phase__in=(
                        "collection",
                        "notifications",
                        "calibration",
                        "retention",
                        "metering",
                    )
                ),
                name="dispatcher_phase_valid",
            ),
        )


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
                fields=("id", "binding"), name="reference_import_binding_identity"
            ),
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
        indexes = (
            models.Index(
                fields=("available_at", "id"),
                condition=models.Q(status__in=("pending", "retry", "running")),
                name="collection_dispatch_due",
            ),
        )
        constraints = (
            models.UniqueConstraint(
                fields=("id", "binding"), name="collection_job_binding_identity"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=("pending", "running", "retry", "succeeded", "failed", "stale")
                ),
                name="collection_job_status",
            ),
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

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=models.Q(job__isnull=True) | models.Q(binding__isnull=False),
                name="request_job_requires_binding",
            ),
            models.CheckConstraint(
                condition=models.Q(outcome__in=("uncertain", "succeeded", "failed")),
                name="request_attempt_outcome",
            ),
        )
        indexes = (
            models.Index(fields=("provider_account", "utc_day"), name="request_account_day"),
        )


class ForecastSnapshot(models.Model):
    job = models.OneToOneField(CollectionJob, on_delete=models.PROTECT)
    binding = models.ForeignKey(SiteBinding, on_delete=models.PROTECT, related_name="snapshots")
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

    class Meta:
        indexes = (
            models.Index(
                fields=("binding", "source_kind", "-collected_at", "-available_at", "-id"),
                name="snapshot_binding_source_age",
            ),
        )
        constraints = (
            models.UniqueConstraint(fields=("id", "binding"), name="snapshot_binding_identity"),
            models.CheckConstraint(
                condition=(
                    models.Q(coverage_start__isnull=True, coverage_end__isnull=True)
                    | models.Q(
                        coverage_start__isnull=False,
                        coverage_end__isnull=False,
                        coverage_end__gt=models.F("coverage_start"),
                    )
                ),
                name="snapshot_coverage_bounds",
            ),
            models.CheckConstraint(
                condition=models.Q(source_kind__in=("unknown", "forecast", "estimated_actuals")),
                name="snapshot_source_kind",
            ),
        )


class ForecastResult(models.Model):
    snapshot = models.OneToOneField(ForecastSnapshot, on_delete=models.PROTECT)
    payload = models.JSONField()
    daily_payloads = models.JSONField(default=dict)
    published_at = models.DateTimeField()


class ReferenceCapacityNormalization(models.Model):
    source_snapshot = models.ForeignKey(
        ForecastSnapshot, on_delete=models.PROTECT, related_name="normalized_copies"
    )
    snapshot = models.OneToOneField(
        ForecastSnapshot, on_delete=models.PROTECT, related_name="capacity_normalization"
    )
    target_binding = models.ForeignKey(SiteBinding, on_delete=models.PROTECT)
    source_capacity_kw = models.FloatField()
    target_capacity_kw = models.FloatField()
    factor = models.FloatField()
    authorized_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    authorization_reference = models.CharField(max_length=1000)
    created_at = models.DateTimeField()

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=~models.Q(snapshot=models.F("source_snapshot")),
                name="normalization_distinct_source",
            ),
            models.CheckConstraint(
                condition=models.Q(source_capacity_kw__gt=0, source_capacity_kw__lt=float("inf")),
                name="normalization_source_capacity",
            ),
            models.CheckConstraint(
                condition=models.Q(target_capacity_kw__gt=0, target_capacity_kw__lt=float("inf")),
                name="normalization_target_capacity",
            ),
            models.UniqueConstraint(
                fields=("source_snapshot", "target_binding"),
                name="unique_reference_capacity_normalization",
            ),
            models.CheckConstraint(
                condition=models.Q(factor__gt=0, factor__lt=float("inf")),
                name="finite_positive_reference_scale",
            ),
        )
