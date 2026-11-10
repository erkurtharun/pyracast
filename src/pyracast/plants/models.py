from datetime import timedelta

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeBoundary, RangeOperators
from django.db import models
from django.db.models import F, Q


class TsTzRange(models.Func):
    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class Plant(models.Model):
    region = models.ForeignKey("regions.Region", on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    current_configuration = models.ForeignKey(
        "PlantConfiguration", null=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)


class PlantConfiguration(models.Model):
    plant = models.ForeignKey(Plant, on_delete=models.PROTECT, related_name="configurations")
    binding = models.ForeignKey("connections.SiteBinding", on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    dc_capacity_mw = models.FloatField()
    inverter_capacity_mw = models.FloatField(null=True)
    export_limit_mw = models.FloatField(null=True)
    measurement_basis = models.CharField(
        max_length=20,
        choices=(("net_export", "Net export"), ("ac_generation", "AC generation")),
        default="net_export",
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("plant", "revision"), name="plant_config_revision_unique"
            ),
            models.CheckConstraint(
                condition=Q(dc_capacity_mw__gt=0, dc_capacity_mw__lt=float("inf")),
                name="plant_dc_capacity_finite",
            ),
            models.CheckConstraint(
                condition=Q(inverter_capacity_mw__isnull=True)
                | Q(inverter_capacity_mw__gt=0, inverter_capacity_mw__lt=float("inf")),
                name="plant_inverter_capacity_finite",
            ),
            models.CheckConstraint(
                condition=Q(export_limit_mw__isnull=True)
                | Q(export_limit_mw__gt=0, export_limit_mw__lt=float("inf")),
                name="plant_export_limit_finite",
            ),
        )


class ProductionObservation(models.Model):
    plant = models.ForeignKey(Plant, on_delete=models.PROTECT)
    configuration = models.ForeignKey(PlantConfiguration, on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    energy_mwh = models.FloatField()
    quality = models.CharField(
        max_length=20,
        choices=(
            ("valid", "Valid"),
            ("outage", "Outage"),
            ("curtailed", "Curtailed"),
            ("invalid", "Invalid"),
        ),
        default="valid",
    )
    source = models.CharField(max_length=200)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("plant", "starts_at", "ends_at"), name="plant_observation_unique"
            ),
            models.CheckConstraint(
                condition=Q(ends_at=F("starts_at") + timedelta(minutes=30))
                | Q(ends_at=F("starts_at") + timedelta(minutes=60)),
                name="plant_observation_duration",
            ),
            models.CheckConstraint(
                condition=Q(energy_mwh__gte=0, energy_mwh__lt=float("inf")),
                name="plant_observation_energy_finite",
            ),
            ExclusionConstraint(
                name="plant_observation_no_overlap",
                expressions=(
                    ("plant", RangeOperators.EQUAL),
                    (TsTzRange("starts_at", "ends_at", RangeBoundary()), RangeOperators.OVERLAPS),
                ),
            ),
        )


class CalibrationRun(models.Model):
    plant = models.ForeignKey(Plant, on_delete=models.PROTECT)
    configuration = models.ForeignKey(PlantConfiguration, on_delete=models.PROTECT)
    binding = models.ForeignKey("connections.SiteBinding", on_delete=models.PROTECT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    window_start = models.DateField()
    window_end = models.DateField()
    cutoff_at = models.DateTimeField()
    observation_id_cutoff = models.PositiveBigIntegerField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)
    status = models.CharField(
        max_length=20,
        choices=tuple(
            (s, s)
            for s in (
                "pending",
                "running",
                "ready",
                "insufficient",
                "rejected",
                "failed",
                "superseded",
            )
        ),
        default="pending",
    )
    assessment = models.CharField(max_length=200, default="")
    factor = models.FloatField(null=True)
    result = models.JSONField(default=dict)
    dataset_manifest = models.JSONField(default=dict)
    dataset_sha256 = models.CharField(max_length=64, default="")
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=200, default="")

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("plant",),
                condition=Q(status__in=("pending", "running")),
                name="plant_one_pending_calibration",
            ),
        )


class CalibrationActivation(models.Model):
    plant = models.ForeignKey(Plant, on_delete=models.PROTECT)
    configuration = models.ForeignKey(PlantConfiguration, on_delete=models.PROTECT)
    calibration = models.ForeignKey(CalibrationRun, null=True, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class PlantForecast(models.Model):
    configuration = models.ForeignKey(PlantConfiguration, on_delete=models.PROTECT)
    snapshot = models.ForeignKey("forecasts.ForecastSnapshot", on_delete=models.PROTECT)
    calibration = models.ForeignKey(CalibrationRun, null=True, on_delete=models.PROTECT)
    resolution_minutes = models.PositiveIntegerField()
    target_date = models.DateField()
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=(
                    "configuration",
                    "snapshot",
                    "calibration",
                    "resolution_minutes",
                    "target_date",
                ),
                nulls_distinct=False,
                name="plant_forecast_unique",
            ),
            models.CheckConstraint(
                condition=Q(resolution_minutes__in=(30, 60)), name="plant_forecast_resolution"
            ),
        )


class PlantEvent(models.Model):
    plant = models.ForeignKey(Plant, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=50)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
