import uuid

from django.conf import settings
from django.db import models
from django.db.models import F
from django.utils.translation import gettext_lazy

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.db import TIME_ITEMS, choices_check, json_shape_check, lease_check
from pyracast.regions.models import Region


class DispatcherPhase(models.TextChoices):
    COLLECTION = "collection", gettext_lazy("Collection")
    NOTIFICATIONS = "notifications", gettext_lazy("Notifications")
    CALIBRATION = "calibration", gettext_lazy("Calibration")
    RETENTION = "retention", gettext_lazy("Retention")
    METERING = "metering", gettext_lazy("Metering")


class CollectionEndpoint(models.TextChoices):
    FORECASTS = "forecasts", "Forecasts"
    ESTIMATED_ACTUALS = "estimated_actuals", "Estimated actuals"
    HISTORICAL_IMPORT = "historical_import", "Historical import"
    SCALED_FORECASTS = "scaled_forecasts", "Scaled forecasts"
    SCALED_ESTIMATED_ACTUALS = "scaled_estimated_actuals", "Scaled estimated actuals"


class CollectionJobStatus(models.TextChoices):
    PENDING = "pending", gettext_lazy("Pending")
    RUNNING = "running", gettext_lazy("Running")
    RETRY = "retry", gettext_lazy("Retry")
    SUCCEEDED = "succeeded", gettext_lazy("Succeeded")
    FAILED = "failed", gettext_lazy("Failed")
    STALE = "stale", gettext_lazy("Stale")


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


class CollectionState(models.TextChoices):
    COLLECTING = "collecting", "Collecting"
    WAITING_FOR_APPROVAL = "waiting_for_approval", "Waiting for approval"
    PAUSED = "paused", "Paused"
    PROVIDER_ATTENTION = "provider_attention", "Waiting for the provider"


class CollectionInactiveReason(models.TextChoices):
    NOT_APPROVED = "not_approved", "Not approved"
    NOT_VALIDATED = "not_validated", "Not validated"
    BINDING_NOT_CURRENT = "binding_not_current", "Binding not current"
    SCHEDULE_DISABLED = "schedule_disabled", "Schedule disabled"
    QUOTA_TOO_LOW = "quota_too_low", "Quota too low for forecasts"
    PROVIDER_UNAVAILABLE = "provider_unavailable", "Provider unavailable"


class CollectionJobError(models.TextChoices):
    HISTORY_WINDOW_CLOSED = "history_window_closed", gettext_lazy("History window closed")
    ATTEMPTS_EXHAUSTED = "attempts_exhausted", gettext_lazy("Attempts exhausted")
    FORECAST_SUPERSEDED = "forecast_superseded", gettext_lazy("Forecast superseded")
    BINDING_NOT_CURRENT = "binding_not_current", gettext_lazy("Binding not current")
    SCHEDULE_DISABLED = "schedule_disabled", gettext_lazy("Schedule disabled")
    SLOT_NOT_CONFIGURED = "slot_not_configured", gettext_lazy("Slot not configured")
    SCHEDULE_CHANGED = "schedule_changed", gettext_lazy("Schedule changed")
    HISTORY_INCOMPLETE = "history_incomplete", gettext_lazy("History incomplete")
    HISTORY_ALREADY_ARCHIVED = "history_already_archived", gettext_lazy("History already archived")
    PROVIDER_AUTHENTICATION_FAILED = (
        "provider_authentication_failed",
        gettext_lazy("Provider authentication failed"),
    )
    PROVIDER_ALLOWANCE_EXHAUSTED = (
        "provider_allowance_exhausted",
        gettext_lazy("Provider allowance exhausted"),
    )
    PROVIDER_ACCESS_DENIED = "provider_access_denied", gettext_lazy("Provider access denied")
    PROVIDER_SITE_NOT_FOUND = "provider_site_not_found", gettext_lazy("Provider site not found")
    PROVIDER_THROTTLED = "provider_throttled", gettext_lazy("Provider throttled")
    PROVIDER_SERVER_ERROR = "provider_server_error", gettext_lazy("Provider server error")
    PROVIDER_UNREACHABLE = "provider_unreachable", gettext_lazy("Provider unreachable")
    PROVIDER_INVALID_RESPONSE = (
        "provider_invalid_response",
        gettext_lazy("Provider invalid response"),
    )
    PROVIDER_CONFIGURATION_INVALID = (
        "provider_configuration_invalid",
        gettext_lazy("Provider configuration invalid"),
    )
    ACCOUNT_UNAVAILABLE = "account_unavailable", gettext_lazy("Account unavailable")
    HISTORY_BUDGET_RESERVED = "history_budget_reserved", gettext_lazy("History budget reserved")
    QUOTA_EXHAUSTED = "quota_exhausted", gettext_lazy("Quota exhausted")
    # Any other provider answer: a status this vocabulary does not name.
    PROVIDER_FAILURE = "provider_failure", gettext_lazy("Provider failure")
    PROVIDER_UNEXPECTED_FAILURE = (
        "provider_unexpected_failure",
        gettext_lazy("Provider unexpected failure"),
    )
    INVALID_SOURCE_KIND = "invalid_source_kind", gettext_lazy("Invalid source kind")
    CALCULATION_FAILURE = "calculation_failure", gettext_lazy("Calculation failure")
    LOCK_CONTENTION = "lock_contention", gettext_lazy("Lock contention")
    PUBLICATION_FAILURE = "publication_failure", gettext_lazy("Publication failure")


class DispatcherStatus(models.Model):
    phase = models.CharField(max_length=20, primary_key=True, choices=DispatcherPhase)
    attempted_at = models.DateTimeField()
    succeeded_at = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=100, blank=True, default="")
    duration_ms = models.PositiveIntegerField(null=True)
    published = models.PositiveIntegerField(null=True)

    class Meta:
        constraints = (choices_check("phase", DispatcherPhase),)

    def __str__(self) -> str:
        return self.phase


class CollectionSchedule(models.Model):
    region = models.OneToOneField(Region, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=True)
    times = models.JSONField(default=list)
    effective_from = models.DateTimeField()

    class Meta:
        constraints = (json_shape_check("times", "array", TIME_ITEMS),)


class HistoricalReferenceImport(models.Model):
    binding = models.ForeignKey(SiteBinding, on_delete=models.PROTECT)
    content_sha256 = models.CharField(max_length=64)
    source_name = models.CharField(max_length=255)
    source_settings = models.JSONField()
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField()
    interval_count = models.PositiveIntegerField()

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("id", "binding"), name="%(class)s_id_binding_uniq"),
            models.UniqueConstraint(
                fields=("binding", "content_sha256"), name="%(class)s_binding_content_sha256_uniq"
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
    scheduled_for = models.DateTimeField()
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
                name="%(class)s_due_idx",
            ),
        )
        constraints = (
            models.UniqueConstraint(fields=("id", "binding"), name="%(class)s_id_binding_uniq"),
            choices_check("endpoint", CollectionEndpoint),
            choices_check("status", CollectionJobStatus),
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
                fields=("binding", "endpoint", "scheduled_for"),
                condition=models.Q(
                    endpoint__in=(
                        CollectionEndpoint.FORECASTS,
                        CollectionEndpoint.ESTIMATED_ACTUALS,
                    )
                ),
                name="%(class)s_binding_endpoint_scheduled_for_uniq",
            ),
            models.UniqueConstraint(
                fields=("reference_import", "target_date"),
                name="%(class)s_reference_import_target_date_uniq",
            ),
        )

    def __str__(self) -> str:
        return f"{self.endpoint} {self.target_date} of binding {self.binding_id}"


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
    http_status = models.PositiveSmallIntegerField(null=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=models.Q(job__isnull=True) | models.Q(binding__isnull=False),
                name="%(class)s_job_binding_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(http_status__isnull=True)
                | models.Q(http_status__gte=100, http_status__lte=599),
                name="%(class)s_http_status_chk",
            ),
            choices_check("purpose", RequestPurpose),
            choices_check("outcome", RequestOutcome),
        )
        indexes = (
            models.Index(fields=("provider_account", "utc_day"), name="%(class)s_account_day_idx"),
        )

    def __str__(self) -> str:
        return f"{self.purpose} request {self.pk}: {self.outcome}"


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

    class Meta:
        indexes = (
            models.Index(
                fields=("binding", "source_kind", "-collected_at", "-available_at", "-id"),
                name="%(class)s_latest_idx",
            ),
        )
        constraints = (
            models.UniqueConstraint(fields=("id", "binding"), name="%(class)s_id_binding_uniq"),
            models.CheckConstraint(
                condition=(
                    models.Q(coverage_start__isnull=True, coverage_end__isnull=True)
                    | models.Q(
                        coverage_start__isnull=False,
                        coverage_end__isnull=False,
                        coverage_end__gt=models.F("coverage_start"),
                    )
                ),
                name="%(class)s_coverage_chk",
            ),
            choices_check("source_kind", SnapshotSourceKind),
        )

    def __str__(self) -> str:
        return f"{self.source_kind} snapshot {self.pk} of binding {self.binding_id}"


class ForecastResult(models.Model):
    snapshot = models.OneToOneField(ForecastSnapshot, on_delete=models.PROTECT)
    payload = models.JSONField()
    daily_payloads = models.JSONField(default=dict)
    published_at = models.DateTimeField()

    def __str__(self) -> str:
        return f"Result of snapshot {self.snapshot_id}"


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
                name="%(class)s_source_snapshot_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(source_capacity_kw__gt=0, source_capacity_kw__lt=float("inf")),
                name="%(class)s_source_capacity_kw_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(target_capacity_kw__gt=0, target_capacity_kw__lt=float("inf")),
                name="%(class)s_target_capacity_kw_chk",
            ),
            models.UniqueConstraint(
                fields=("source_snapshot", "target_binding"),
                name="%(class)s_source_target_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(factor__gt=0, factor__lt=float("inf")),
                name="%(class)s_factor_finite_chk",
            ),
            # The writer stores target / source; Python and PostgreSQL divide float8 identically
            # (IEEE 754), so the stored factor is the exact quotient at every capacity.
            # PostgreSQL tests CHECK constraints in name order: this name sorts after the
            # capacity checks, which reject the zero this division would raise on.
            models.CheckConstraint(
                condition=models.Q(factor=F("target_capacity_kw") / F("source_capacity_kw")),
                name="%(class)s_target_over_source_chk",
            ),
        )
