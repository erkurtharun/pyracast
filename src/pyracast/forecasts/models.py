import uuid

from django.conf import settings
from django.db import models
from django.db.models import F

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.db import choices_check, lease_check
from pyracast.regions.models import Region


class DispatcherPhase(models.TextChoices):
    COLLECTION = "collection", "Collection"
    NOTIFICATIONS = "notifications", "Notifications"
    CALIBRATION = "calibration", "Calibration"
    RETENTION = "retention", "Retention"
    METERING = "metering", "Metering"


class CollectionEndpoint(models.TextChoices):
    FORECASTS = "forecasts", "Forecasts"
    ESTIMATED_ACTUALS = "estimated_actuals", "Estimated actuals"
    HISTORICAL_IMPORT = "historical_import", "Historical import"
    SCALED_FORECASTS = "scaled_forecasts", "Scaled forecasts"
    SCALED_ESTIMATED_ACTUALS = "scaled_estimated_actuals", "Scaled estimated actuals"


class CollectionJobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    RETRY = "retry", "Retry"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    STALE = "stale", "Stale"


class RequestPurpose(models.TextChoices):
    FORECAST = "forecast", "Forecast"
    ESTIMATED_ACTUALS = "estimated_actuals", "Estimated actuals"
    VALIDATION = "validation", "Validation"
    INSPECT_SETTINGS = "inspect_settings", "Site settings check"
    INSPECT_FORECAST = "inspect_forecast", "Forecast check"


class RequestOutcome(models.TextChoices):
    UNCERTAIN = "uncertain", "Uncertain"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


class SnapshotSourceKind(models.TextChoices):
    FORECAST = "forecast", "Forecast"
    ESTIMATED_ACTUALS = "estimated_actuals", "Estimated actuals"


class CollectionJobError(models.TextChoices):
    HISTORY_WINDOW_CLOSED = "history_window_closed", "History window closed"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted", "Attempts exhausted"
    FORECAST_SUPERSEDED = "forecast_superseded", "Forecast superseded"
    HISTORY_INCOMPLETE = "history_incomplete", "History incomplete"
    HISTORY_ALREADY_ARCHIVED = "history_already_archived", "History already archived"
    PROVIDER_AUTHENTICATION_FAILED = (
        "provider_authentication_failed",
        "Provider authentication failed",
    )
    PROVIDER_THROTTLED = "provider_throttled", "Provider throttled"
    HISTORY_BUDGET_RESERVED = "history_budget_reserved", "History budget reserved"
    QUOTA_EXHAUSTED = "quota_exhausted", "Quota exhausted"
    PROVIDER_FAILURE = "provider_failure", "Provider failure"
    PROVIDER_UNEXPECTED_FAILURE = "provider_unexpected_failure", "Provider unexpected failure"
    INVALID_SOURCE_KIND = "invalid_source_kind", "Invalid source kind"
    CALCULATION_FAILURE = "calculation_failure", "Calculation failure"


class SnapshotWeatherError(models.TextChoices):
    # Every WeatherFailure code is a member: weather collection stores WeatherError.code here.
    INVALID_NUMBER = "invalid_weather_number", "Invalid weather number"
    INVALID_COORDINATES = "invalid_weather_coordinates", "Invalid weather coordinates"
    INVALID_SCHEMA = "invalid_weather_schema", "Invalid weather schema"
    INVALID_TIMEZONE = "invalid_weather_timezone", "Invalid weather timezone"
    INVALID_UNITS = "invalid_weather_units", "Invalid weather units"
    EMPTY_RESPONSE = "empty_weather_response", "Empty weather response"
    INVALID_INTERVAL = "invalid_weather_interval", "Invalid weather interval"
    INVALID_WINDOW = "invalid_weather_window", "Invalid weather window"
    UNSAFE_RESPONSE = "unsafe_weather_response", "Unsafe weather response"
    INVALID_COLLECTION_TIME = "invalid_weather_collection_time", "Invalid weather collection time"
    REQUEST_FAILED = "weather_request_failed", "Weather request failed"
    ACQUISITION_FAILED = "weather_acquisition_failed", "Weather acquisition failed"
    ACQUISITION_INTERRUPTED = "weather_acquisition_interrupted", "Weather acquisition interrupted"


class DispatcherStatus(models.Model):
    phase = models.CharField(max_length=20, primary_key=True, choices=DispatcherPhase)
    attempted_at = models.DateTimeField()
    succeeded_at = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=100, blank=True, default="")
    duration_ms = models.PositiveIntegerField(null=True)
    published = models.PositiveIntegerField(null=True)

    class Meta:
        constraints = (choices_check("phase", DispatcherPhase, name="dispatcher_phase_valid"),)


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
    endpoint = models.CharField(
        max_length=30, choices=CollectionEndpoint, default=CollectionEndpoint.FORECASTS
    )
    scheduled_slot = models.DateTimeField()
    target_date = models.DateField()
    status = models.CharField(
        max_length=20, choices=CollectionJobStatus, default=CollectionJobStatus.PENDING
    )
    available_at = models.DateTimeField()
    retry_until = models.DateTimeField(null=True)
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=40, choices=CollectionJobError, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = (
            models.Index(
                fields=("available_at", "id"),
                condition=models.Q(
                    status__in=(
                        CollectionJobStatus.PENDING,
                        CollectionJobStatus.RETRY,
                        CollectionJobStatus.RUNNING,
                    )
                ),
                name="collection_dispatch_due",
            ),
        )
        constraints = (
            models.UniqueConstraint(
                fields=("id", "binding"), name="collection_job_binding_identity"
            ),
            choices_check("endpoint", CollectionEndpoint),
            choices_check("status", CollectionJobStatus, name="collection_job_status"),
            choices_check("last_error", CollectionJobError, blank=True),
            lease_check((CollectionJobStatus.RUNNING,)),
            models.CheckConstraint(
                condition=models.Q(
                    endpoint=CollectionEndpoint.HISTORICAL_IMPORT, reference_import__isnull=False
                )
                | (
                    ~models.Q(endpoint=CollectionEndpoint.HISTORICAL_IMPORT)
                    & models.Q(reference_import__isnull=True)
                ),
                name="%(class)s_reference_import_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(reference_import__isnull=True)
                | models.Q(status=CollectionJobStatus.SUCCEEDED),
                name="%(class)s_import_status_chk",
            ),
            # Imports and scaled copies take their slot from their source, so only native
            # acquisitions own one slot per binding and endpoint.
            models.UniqueConstraint(
                fields=("binding", "endpoint", "scheduled_slot"),
                condition=models.Q(
                    endpoint__in=(
                        CollectionEndpoint.FORECASTS,
                        CollectionEndpoint.ESTIMATED_ACTUALS,
                    )
                ),
                name="%(class)s_binding_endpoint_scheduled_slot_uniq",
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
    purpose = models.CharField(max_length=20, choices=RequestPurpose)
    outcome = models.CharField(
        max_length=20, choices=RequestOutcome, default=RequestOutcome.UNCERTAIN
    )
    correlation_id = models.UUIDField(default=uuid.uuid4, unique=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=models.Q(job__isnull=True) | models.Q(binding__isnull=False),
                name="request_job_requires_binding",
            ),
            choices_check("purpose", RequestPurpose),
            choices_check("outcome", RequestOutcome, name="request_attempt_outcome"),
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
    source_kind = models.CharField(max_length=20, choices=SnapshotSourceKind)
    coverage_start = models.DateTimeField(null=True, db_index=True)
    coverage_end = models.DateTimeField(null=True, db_index=True)
    available_at = models.DateTimeField(null=True)
    weather_payload = models.JSONField(null=True)
    weather_error = models.CharField(
        max_length=64, choices=SnapshotWeatherError, blank=True, default=""
    )

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
            choices_check("source_kind", SnapshotSourceKind, name="snapshot_source_kind"),
            choices_check("weather_error", SnapshotWeatherError, blank=True),
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
            # The writer stores target / source; Python and PostgreSQL divide float8 identically
            # (IEEE 754), so the stored factor is the exact quotient at every capacity.
            models.CheckConstraint(
                condition=models.Q(factor=F("target_capacity_kw") / F("source_capacity_kw")),
                name="%(class)s_factor_chk",
            ),
        )
