from django.conf import settings
from django.db import models
from django.db.models import F, Q

from pyracast.db import choices_check


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
            models.UniqueConstraint(fields=("id", "plant"), name="epias_connection_plant_identity"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="epias_revision_positive"),
        )


class EpiasCollectionJob(models.Model):
    connection = models.ForeignKey(EpiasConnection, on_delete=models.PROTECT)
    plant = models.ForeignKey("plants.Plant", on_delete=models.PROTECT)
    configuration = models.ForeignKey("plants.PlantConfiguration", on_delete=models.PROTECT)
    connection_revision = models.PositiveIntegerField()
    provider_plant = models.JSONField()
    target_day = models.DateField()
    period_end_day = models.DateField(null=True)
    source_kind = models.CharField(
        max_length=30, choices=EpiasSourceKind, default=EpiasSourceKind.REALTIME_GENERATION
    )
    scheduled_for = models.DateTimeField()
    status = models.CharField(max_length=20, choices=EpiasJobStatus, default=EpiasJobStatus.PENDING)
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    retry_until = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=40, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=(
                    "connection",
                    "connection_revision",
                    "configuration",
                    "source_kind",
                    "target_day",
                    "scheduled_for",
                ),
                name="epias_unique_collection_slot",
            ),
            models.UniqueConstraint(
                fields=("id", "connection", "configuration"), name="epias_job_lineage_unique"
            ),
            models.CheckConstraint(
                condition=Q(period_end_day__isnull=True) | Q(period_end_day__gte=F("target_day")),
                name="epias_job_period_valid",
            ),
            models.CheckConstraint(
                condition=Q(connection_revision__gte=1), name="epias_job_revision_positive"
            ),
            choices_check("source_kind", EpiasSourceKind, name="epias_job_source_valid"),
            choices_check("status", EpiasJobStatus, name="epias_job_status_valid"),
        )
        indexes = (models.Index(fields=("status", "next_attempt_at")),)


class EpiasSnapshot(models.Model):
    job = models.ForeignKey(EpiasCollectionJob, on_delete=models.PROTECT)
    attempt = models.PositiveSmallIntegerField()
    connection = models.ForeignKey(EpiasConnection, on_delete=models.PROTECT)
    configuration = models.ForeignKey("plants.PlantConfiguration", on_delete=models.PROTECT)
    target_day = models.DateField()
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
                fields=("job", "attempt", "target_day"), name="epias_unique_job_acquisition"
            ),
            models.CheckConstraint(
                condition=Q(attempt__gte=1), name="epias_acquisition_attempt_positive"
            ),
            choices_check("source_kind", EpiasSourceKind, name="epias_acquisition_source_valid"),
        )
        indexes = (models.Index(fields=("connection", "target_day", "available_at")),)
