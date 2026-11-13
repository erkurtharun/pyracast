from datetime import timedelta

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeBoundary, RangeOperators
from django.db import models
from django.db.models import F, Q

from pyracast.plants.measurement_policy import MEASUREMENT_POLICY

MEASUREMENT_BASES = (("net_export", "Net export"), ("ac_generation", "AC generation"))
MOUNTING_TYPES = (("unknown", "Unknown"), ("fixed", "Fixed"), ("tracking", "Tracking"))


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
    deleted_at = models.DateTimeField(null=True, db_index=True)
    automatic_calibration_enabled = models.BooleanField(default=False)
    automatic_calibration_enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    next_calibration_check_at = models.DateTimeField(null=True, db_index=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("id", "region"), name="plant_region_identity"),
            models.UniqueConstraint(
                fields=("region",),
                condition=Q(deleted_at__isnull=True),
                name="one_active_plant_per_region",
            ),
        )


class PlantSubmission(models.Model):
    plant = models.OneToOneField(Plant, on_delete=models.CASCADE, related_name="submission")
    dc_capacity_mw = models.FloatField()
    inverter_capacity_mw = models.FloatField(null=True)
    export_limit_mw = models.FloatField(null=True)
    measurement_basis = models.CharField(
        max_length=20,
        choices=MEASUREMENT_BASES,
        default="net_export",
    )
    mounting_type = models.CharField(
        max_length=10,
        choices=MOUNTING_TYPES,
        default="unknown",
    )
    surface_tilt = models.FloatField(null=True)
    surface_azimuth = models.FloatField(null=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=Q(dc_capacity_mw__gt=0, dc_capacity_mw__lt=float("inf")),
                name="submission_dc_capacity_finite",
            ),
            models.CheckConstraint(
                condition=Q(inverter_capacity_mw__isnull=True)
                | Q(inverter_capacity_mw__gt=0, inverter_capacity_mw__lt=float("inf")),
                name="submission_inverter_finite",
            ),
            models.CheckConstraint(
                condition=Q(export_limit_mw__isnull=True)
                | Q(export_limit_mw__gt=0, export_limit_mw__lt=float("inf")),
                name="submission_export_finite",
            ),
            models.CheckConstraint(
                condition=Q(measurement_basis__in=("net_export", "ac_generation")),
                name="submission_measurement_basis",
            ),
            models.CheckConstraint(
                condition=Q(mounting_type__in=("unknown", "fixed", "tracking")),
                name="submission_mounting_supported",
            ),
            models.CheckConstraint(
                condition=Q(surface_tilt__isnull=True)
                | Q(surface_tilt__gte=0, surface_tilt__lte=90),
                name="submission_tilt_valid",
            ),
            models.CheckConstraint(
                condition=Q(surface_azimuth__isnull=True)
                | Q(surface_azimuth__gte=0, surface_azimuth__lt=360),
                name="submission_azimuth_valid",
            ),
            models.CheckConstraint(
                condition=Q(mounting_type="fixed")
                | Q(surface_tilt__isnull=True, surface_azimuth__isnull=True),
                name="submission_angles_require_fixed",
            ),
        )


class PlantConfiguration(models.Model):
    plant = models.ForeignKey(Plant, on_delete=models.PROTECT, related_name="configurations")
    region = models.ForeignKey("regions.Region", on_delete=models.PROTECT)
    binding = models.ForeignKey("connections.SiteBinding", on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    dc_capacity_mw = models.FloatField()
    inverter_capacity_mw = models.FloatField(null=True)
    export_limit_mw = models.FloatField(null=True)
    mounting_type = models.CharField(
        max_length=10,
        choices=MOUNTING_TYPES,
        default="unknown",
    )
    surface_tilt = models.FloatField(null=True)
    surface_azimuth = models.FloatField(null=True)
    measurement_basis = models.CharField(
        max_length=20,
        choices=MEASUREMENT_BASES,
        default="net_export",
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("plant", "revision"), name="plant_config_revision_unique"
            ),
            models.UniqueConstraint(fields=("id", "plant"), name="plant_config_id_plant_unique"),
            models.UniqueConstraint(
                fields=("id", "binding"), name="plant_config_id_binding_unique"
            ),
            models.CheckConstraint(
                condition=Q(revision__gte=1), name="plant_config_revision_positive"
            ),
            models.CheckConstraint(
                condition=Q(measurement_basis__in=("net_export", "ac_generation")),
                name="plant_measurement_basis_valid",
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
            models.CheckConstraint(
                condition=Q(mounting_type__in=("unknown", "fixed", "tracking")),
                name="plant_mounting_type_supported",
            ),
            models.CheckConstraint(
                condition=Q(surface_tilt__isnull=True)
                | Q(surface_tilt__gte=0, surface_tilt__lte=90),
                name="plant_surface_tilt_valid",
            ),
            models.CheckConstraint(
                condition=Q(surface_azimuth__isnull=True)
                | Q(surface_azimuth__gte=0, surface_azimuth__lt=360),
                name="plant_surface_azimuth_valid",
            ),
            models.CheckConstraint(
                condition=Q(mounting_type="fixed")
                | Q(surface_tilt__isnull=True, surface_azimuth__isnull=True),
                name="plant_angles_require_fixed",
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
            models.CheckConstraint(
                condition=Q(quality__in=("valid", "outage", "curtailed", "invalid")),
                name="plant_observation_quality_valid",
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
    comparison_activation = models.ForeignKey(
        "CalibrationActivation", null=True, on_delete=models.PROTECT, related_name="comparison_runs"
    )
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    window_start = models.DateField()
    window_end = models.DateField()
    cutoff_at = models.DateTimeField()
    observation_id_cutoff = models.PositiveBigIntegerField(null=True)
    production_snapshot_ids = models.JSONField(default=list)
    measurement_selection_policy = models.CharField(max_length=80, default=MEASUREMENT_POLICY)
    reference_snapshot_ids = models.JSONField(null=True, default=None)
    incident_revision_ids = models.JSONField(default=list)
    input_fingerprint = models.CharField(max_length=64, default="")
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
            models.UniqueConstraint(
                fields=("id", "configuration"), name="calibration_id_config_unique"
            ),
            models.CheckConstraint(
                condition=Q(window_end__gte=F("window_start")), name="calibration_window_valid"
            ),
            models.CheckConstraint(
                condition=Q(factor__isnull=True) | Q(factor__gt=0, factor__lt=float("inf")),
                name="calibration_factor_finite",
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=(
                        "pending",
                        "running",
                        "ready",
                        "insufficient",
                        "rejected",
                        "failed",
                        "superseded",
                    )
                ),
                name="calibration_status_valid",
            ),
        )


class CalibrationActivation(models.Model):
    plant = models.ForeignKey(Plant, on_delete=models.PROTECT)
    configuration = models.ForeignKey(PlantConfiguration, on_delete=models.PROTECT)
    calibration = models.ForeignKey(CalibrationRun, null=True, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("id", "configuration"), name="activation_id_config_unique"
            ),
        )


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
