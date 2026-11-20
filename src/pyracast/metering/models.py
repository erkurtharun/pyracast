from django.conf import settings
from django.db import models
from django.db.models import F, Q

from pyracast.db import choices_check, lease_check


class EpiasSourceKind(models.TextChoices):
    REALTIME_GENERATION = "realtime_generation", "Real-time generation"
    SETTLEMENT_INJECTION = "settlement_injection", "Settlement injection"


class EpiasJobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    SUPERSEDED = "superseded", "Superseded"
    AWAITING_PUBLICATION = "awaiting_publication", "Awaiting publication"


class EpiasConnection(models.Model):
    plant = models.OneToOneField("plants.Plant", on_delete=models.PROTECT)
    provider_plant_id = models.PositiveBigIntegerField()
    settlement_plant_id = models.PositiveBigIntegerField(null=True)
    settlement_checked_at = models.DateTimeField(null=True)
    eic = models.CharField(max_length=100)
    name = models.CharField(max_length=300)
    enabled = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)
    connected_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("id", "plant"), name="%(class)s_id_plant_uniq"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="%(class)s_revision_chk"),
        )


class EpiasConnectionRevision(models.Model):
    """The provider identity a connection collected under from ``connected_at`` on."""

    connection = models.ForeignKey(EpiasConnection, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    provider_plant_id = models.PositiveBigIntegerField()
    eic = models.CharField(max_length=100)
    connected_at = models.DateTimeField()

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("connection", "revision"), name="%(class)s_connection_revision_uniq"
            ),
            models.CheckConstraint(condition=Q(revision__gte=1), name="%(class)s_revision_chk"),
            models.CheckConstraint(
                condition=Q(provider_plant_id__gte=1), name="%(class)s_provider_plant_id_chk"
            ),
            models.CheckConstraint(condition=~Q(eic=""), name="%(class)s_eic_chk"),
        )


class EpiasJobError(models.TextChoices):
    RETRY_WINDOW_EXPIRED = "retry_window_expired", "Retry window expired"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted", "Attempts exhausted"
    PROVIDER_FAILURE = "epias_provider_failure", "Epias provider failure"
    CREDENTIALS_MISSING = "epias_credentials_missing", "EPIAS credentials missing"
    CREDENTIALS_REJECTED = "epias_credentials_rejected", "EPIAS credentials rejected"
    UNPUBLISHED_DATA = "unpublished_data", "Unpublished data"
    PARTIAL_DATA = "partial_data", "Partial data"


class EpiasCollectionJob(models.Model):
    connection = models.ForeignKey(EpiasConnection, on_delete=models.PROTECT)
    plant = models.ForeignKey("plants.Plant", on_delete=models.PROTECT)
    configuration = models.ForeignKey("plants.PlantConfiguration", on_delete=models.PROTECT)
    connection_revision = models.PositiveIntegerField()
    provider_plant = models.JSONField()
    target_date = models.DateField()
    period_end_day = models.DateField(null=True)
    source_kind = models.CharField(
        max_length=30, choices=EpiasSourceKind, default=EpiasSourceKind.REALTIME_GENERATION
    )
    scheduled_for = models.DateTimeField()
    status = models.CharField(max_length=20, choices=EpiasJobStatus, default=EpiasJobStatus.PENDING)
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField()
    retry_until = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=40, choices=EpiasJobError, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=(
                    "connection",
                    "connection_revision",
                    "configuration",
                    "source_kind",
                    "target_date",
                    "scheduled_for",
                ),
                name="%(class)s_slot_uniq",
            ),
            models.UniqueConstraint(
                fields=("id", "connection", "configuration"),
                name="%(class)s_id_connection_configuration_uniq",
            ),
            models.CheckConstraint(
                condition=Q(period_end_day__isnull=True) | Q(period_end_day__gte=F("target_date")),
                name="%(class)s_period_end_day_chk",
            ),
            models.CheckConstraint(
                condition=Q(connection_revision__gte=1), name="%(class)s_connection_revision_chk"
            ),
            choices_check("source_kind", EpiasSourceKind),
            choices_check("status", EpiasJobStatus),
            choices_check("last_error", EpiasJobError, blank=True),
            lease_check((EpiasJobStatus.RUNNING,)),
        )
        indexes = (models.Index(fields=("status", "available_at"), name="%(class)s_due_idx"),)


class EpiasSnapshot(models.Model):
    job = models.ForeignKey(EpiasCollectionJob, on_delete=models.PROTECT)
    attempt = models.PositiveSmallIntegerField()
    connection = models.ForeignKey(EpiasConnection, on_delete=models.PROTECT)
    configuration = models.ForeignKey("plants.PlantConfiguration", on_delete=models.PROTECT)
    target_date = models.DateField()
    source_kind = models.CharField(
        max_length=30, choices=EpiasSourceKind, default=EpiasSourceKind.REALTIME_GENERATION
    )
    provider_plant = models.JSONField(default=dict)
    endpoint = models.URLField(default="")
    normalization_policy = models.CharField(max_length=40, default="hour-start-istanbul-v1")
    collected_at = models.DateTimeField()
    available_at = models.DateTimeField()
    raw_payload = models.JSONField()
    readings = models.JSONField()
    content_sha256 = models.CharField(max_length=64)
    complete = models.BooleanField(default=False)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("job", "attempt", "target_date"),
                name="%(class)s_job_attempt_target_date_uniq",
            ),
            models.CheckConstraint(condition=Q(attempt__gte=1), name="%(class)s_attempt_chk"),
            choices_check("source_kind", EpiasSourceKind),
        )
        indexes = (
            models.Index(
                fields=("connection", "target_date", "available_at"),
                name="%(class)s_connection_idx",
            ),
        )
