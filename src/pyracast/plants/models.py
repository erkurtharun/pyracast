from datetime import timedelta

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeBoundary, RangeOperators
from django.db import models
from django.db.models import F, Q

from pyracast.db import choices_check, lease_check
from pyracast.plants.measurement_policy import MEASUREMENT_POLICY


class MeasurementBasis(models.TextChoices):
    NET_EXPORT = "net_export", "Net export"
    AC_GENERATION = "ac_generation", "AC generation"


class MountingType(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    FIXED = "fixed", "Fixed"
    TRACKING = "tracking", "Tracking"


class ObservationQuality(models.TextChoices):
    VALID = "valid", "Valid"
    OUTAGE = "outage", "Outage"
    CURTAILED = "curtailed", "Curtailed"
    INVALID = "invalid", "Invalid"


class CalibrationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    READY = "ready", "Ready"
    INSUFFICIENT = "insufficient", "Insufficient"
    REJECTED = "rejected", "Rejected"
    FAILED = "failed", "Failed"
    SUPERSEDED = "superseded", "Superseded"


class CalibrationError(models.TextChoices):
    ATTEMPT_LIMIT = "attempt_limit", "Attempt limit reached"
    INVALID_CUTOFF = "invalid_cutoff", "Invalid cutoff"
    INVALID_CALIBRATION_DATA = "invalid_calibration_data", "Invalid calibration data"


class CalibrationAssessment(models.TextChoices):
    INSUFFICIENT = "insufficient", "Insufficient"
    PROVISIONAL = "provisional", "Provisional"
    EVALUATED = "evaluated", "Evaluated"


class PlantEventAction(models.TextChoices):
    SUBMISSION_SAVED = "submission_saved", "Submission saved"
    CONFIGURATION_CREATED = "configuration_created", "Configuration created"
    PLANT_RENAMED = "plant_renamed", "Plant renamed"
    OBSERVATIONS_IMPORTED = "observations_imported", "Observations imported"
    PRODUCTION_CHANGED = "production_changed", "Production changed"
    EPIAS_CONNECTED = "epias_connected", "EPIAS connected"
    EPIAS_DISCONNECTED = "epias_disconnected", "EPIAS disconnected"
    CALIBRATION_REQUESTED = "calibration_requested", "Calibration requested"
    CALIBRATION_SCHEDULED = "calibration_scheduled", "Calibration scheduled"
    CALIBRATION_COMPLETED = "calibration_completed", "Calibration completed"
    CALIBRATION_ACTIVATED = "calibration_activated", "Calibration activated"
    CALIBRATION_AUTOMATICALLY_ACTIVATED = (
        "calibration_automatically_activated",
        "Calibration automatically activated",
    )
    CALIBRATION_RESET = "calibration_reset", "Calibration reset"
    CALIBRATION_AUTOMATION_ENABLED = (
        "calibration_automation_enabled",
        "Automatic calibration enabled",
    )
    CALIBRATION_AUTOMATION_DISABLED = (
        "calibration_automation_disabled",
        "Automatic calibration disabled",
    )
    CALIBRATION_AUTOMATIC_SKIPPED = (
        "calibration_automatic_skipped",
        "Automatic calibration skipped",
    )


CONFIGURATION_PARAMETERS = (
    "dc_capacity_mw",
    "inverter_capacity_mw",
    "export_limit_mw",
    "measurement_basis",
    "mounting_type",
    "surface_tilt",
    "surface_azimuth",
)


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
    automatic_calibration_enabled = models.BooleanField(default=False)
    automatic_calibration_enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    next_calibration_check_at = models.DateTimeField(null=True, db_index=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("id", "region"), name="plant_region_identity"),
            models.UniqueConstraint(fields=("region",), name="one_plant_per_region"),
            models.CheckConstraint(
                condition=Q(
                    automatic_calibration_enabled=True,
                    automatic_calibration_enabled_by__isnull=False,
                    next_calibration_check_at__isnull=False,
                )
                | Q(
                    automatic_calibration_enabled=False,
                    automatic_calibration_enabled_by__isnull=True,
                    next_calibration_check_at__isnull=True,
                ),
                name="%(class)s_automation_chk",
            ),
        )


class PlantSubmission(models.Model):
    plant = models.OneToOneField(Plant, on_delete=models.CASCADE, related_name="submission")
    dc_capacity_mw = models.FloatField()
    inverter_capacity_mw = models.FloatField(null=True)
    export_limit_mw = models.FloatField(null=True)
    measurement_basis = models.CharField(
        max_length=20, choices=MeasurementBasis, default=MeasurementBasis.NET_EXPORT
    )
    mounting_type = models.CharField(
        max_length=10, choices=MountingType, default=MountingType.UNKNOWN
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
            choices_check(
                "measurement_basis", MeasurementBasis, name="submission_measurement_basis"
            ),
            choices_check("mounting_type", MountingType, name="submission_mounting_supported"),
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
                condition=Q(mounting_type=MountingType.FIXED)
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
        max_length=10, choices=MountingType, default=MountingType.UNKNOWN
    )
    surface_tilt = models.FloatField(null=True)
    surface_azimuth = models.FloatField(null=True)
    measurement_basis = models.CharField(
        max_length=20, choices=MeasurementBasis, default=MeasurementBasis.NET_EXPORT
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
            choices_check(
                "measurement_basis", MeasurementBasis, name="plant_measurement_basis_valid"
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
            choices_check("mounting_type", MountingType, name="plant_mounting_type_supported"),
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
                condition=Q(mounting_type=MountingType.FIXED)
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
        max_length=20, choices=ObservationQuality, default=ObservationQuality.VALID
    )
    source = models.CharField(max_length=200)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(auto_now_add=True)
    supersedes = models.OneToOneField(
        "self", null=True, on_delete=models.RESTRICT, related_name="replacement"
    )
    is_current = models.BooleanField(default=True)
    is_reverted = models.BooleanField(default=False)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("plant", "starts_at", "ends_at"),
                condition=Q(is_current=True),
                name="plant_observation_unique",
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
            choices_check("quality", ObservationQuality, name="plant_observation_quality_valid"),
            ExclusionConstraint(
                name="plant_observation_no_overlap",
                condition=Q(is_current=True, is_reverted=False),
                expressions=(
                    ("plant", RangeOperators.EQUAL),
                    (TsTzRange("starts_at", "ends_at", RangeBoundary()), RangeOperators.OVERLAPS),
                ),
            ),
            models.CheckConstraint(
                condition=Q(is_reverted=False) | Q(supersedes__isnull=False),
                name="plant_observation_revert_has_source",
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
        max_length=20, choices=CalibrationStatus, default=CalibrationStatus.PENDING
    )
    assessment = models.CharField(
        max_length=200, choices=CalibrationAssessment, blank=True, default=""
    )
    factor = models.FloatField(null=True)
    result = models.JSONField(default=dict)
    dataset_manifest = models.JSONField(default=dict)
    dataset_sha256 = models.CharField(max_length=64, default="")
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=200, choices=CalibrationError, blank=True, default="")

    class Meta:
        constraints = (
            choices_check("last_error", CalibrationError, blank=True),
            models.UniqueConstraint(
                fields=("plant",),
                condition=Q(status__in=(CalibrationStatus.PENDING, CalibrationStatus.RUNNING)),
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
            choices_check("status", CalibrationStatus, name="calibration_status_valid"),
            lease_check((CalibrationStatus.RUNNING,)),
            models.CheckConstraint(
                condition=Q(status__in=(CalibrationStatus.PENDING, CalibrationStatus.RUNNING))
                | Q(completed_at__isnull=False),
                name="%(class)s_completed_at_chk",
            ),
            choices_check("assessment", CalibrationAssessment, blank=True),
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


class ForecastIssuance(models.Model):
    plant = models.ForeignKey(Plant, on_delete=models.PROTECT)
    configuration = models.ForeignKey(PlantConfiguration, on_delete=models.PROTECT)
    snapshot = models.ForeignKey("forecasts.ForecastSnapshot", on_delete=models.PROTECT)
    calibration = models.ForeignKey(CalibrationRun, null=True, on_delete=models.PROTECT)
    target_date = models.DateField()
    published_at = models.DateTimeField()
    cutoff_at = models.DateTimeField()
    model_version = models.CharField(max_length=100)
    factor = models.FloatField()
    payloads = models.JSONField()
    outage_scenarios = models.JSONField()
    native_intervals = models.JSONField()

    class Meta:
        indexes = (models.Index(fields=("plant", "target_date"), name="issuance_plant_day"),)
        constraints = (
            models.UniqueConstraint(
                fields=("plant", "snapshot", "target_date"), name="forecast_issuance_unique"
            ),
            models.CheckConstraint(
                condition=Q(factor__gt=0, factor__lt=float("inf")),
                name="forecast_issuance_factor_finite",
            ),
            models.CheckConstraint(
                condition=Q(published_at__lt=models.F("cutoff_at")),
                name="forecast_issuance_before_daylight",
            ),
            models.CheckConstraint(
                condition=Q(calibration__isnull=False) | Q(factor=1),
                name="issuance_baseline_factor_identity",
            ),
        )


class PlantEvent(models.Model):
    plant = models.ForeignKey(Plant, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=50, choices=PlantEventAction)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (choices_check("action", PlantEventAction),)
