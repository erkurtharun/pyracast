from django.conf import settings
from django.db import models


class EpiasConnection(models.Model):
    plant = models.OneToOneField("plants.Plant", on_delete=models.CASCADE)
    provider_plant_id = models.PositiveBigIntegerField()
    eic = models.CharField(max_length=100)
    name = models.CharField(max_length=300)
    enabled = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)
    connected_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    meter_basis = models.CharField(
        max_length=20,
        choices=tuple((s, s) for s in ("unknown", "net_export", "ac_generation")),
        default="unknown",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class EpiasCollectionJob(models.Model):
    connection = models.ForeignKey(EpiasConnection, on_delete=models.CASCADE)
    configuration = models.ForeignKey("plants.PlantConfiguration", on_delete=models.CASCADE)
    connection_revision = models.PositiveIntegerField()
    provider_plant = models.JSONField()
    target_day = models.DateField()
    scheduled_for = models.DateTimeField()
    status = models.CharField(max_length=20, default="pending")
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    last_error = models.CharField(max_length=40, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=(
                    "connection",
                    "connection_revision",
                    "configuration",
                    "target_day",
                    "scheduled_for",
                ),
                name="epias_unique_collection_slot",
            ),
        )
        indexes = (models.Index(fields=("status", "next_attempt_at")),)


class EpiasSnapshot(models.Model):
    job = models.ForeignKey(EpiasCollectionJob, on_delete=models.CASCADE)
    attempt = models.PositiveSmallIntegerField()
    connection = models.ForeignKey(EpiasConnection, on_delete=models.CASCADE)
    configuration = models.ForeignKey("plants.PlantConfiguration", on_delete=models.CASCADE)
    target_day = models.DateField()
    collected_at = models.DateTimeField()
    available_at = models.DateTimeField()
    raw_payload = models.JSONField()
    readings = models.JSONField()
    content_sha256 = models.CharField(max_length=64)
    complete = models.BooleanField(default=False)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("job", "attempt"), name="epias_unique_job_acquisition"),
        )
        indexes = (models.Index(fields=("connection", "target_day", "available_at")),)
