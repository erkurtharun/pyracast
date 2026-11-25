from dataclasses import fields as dataclass_fields
from typing import Any

from django.db import models
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from pyracast.api.serializers import (
    DaylightOutput,
    FiniteFloat,
    ForecastEnvelope,
    ForecastProvenanceOutput,
    InputSerializer,
    IrradianceSummaryOutput,
)
from pyracast.change_requests.models import ProductionChangeAction
from pyracast.errors import UNEXPECTED, ErrorCode
from pyracast.plants.activation import ActivationBlocker, SelectionState, selection_state
from pyracast.plants.automation_state import AutomationCause
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationAssessment,
    CalibrationError,
    CalibrationRun,
    MeasurementBasis,
    MountingType,
    ObservationQuality,
    Plant,
    PlantConfiguration,
    PlantSubmission,
    ProductionObservation,
)
from pyracast.plants.parameters import (
    AZIMUTH_LIMIT_DEGREES,
    TILT_MAX_DEGREES,
    PlantParameters,
    declaration,
)
from pyracast.plants.performance import DayReason, SelectionPolicy
from pyracast.plants.production_changes import PRODUCTION_BATCH_LIMIT
from pyracast.regions.models import NAME_MAX_LENGTH


class AwareDateTime(serializers.DateTimeField):
    def to_internal_value(self, value: Any) -> Any:
        if not isinstance(value, str):
            raise serializers.ValidationError(UNEXPECTED)
        try:
            parsed = parse_datetime(value)
        except ValueError:
            parsed = None
        if parsed is None or parsed.utcoffset() is None:
            raise serializers.ValidationError(UNEXPECTED)
        return super().to_internal_value(value)


class PlantInput(InputSerializer):
    name = serializers.CharField(max_length=NAME_MAX_LENGTH)
    dc_capacity_mw = FiniteFloat(min_value=0, above=0)
    inverter_capacity_mw = FiniteFloat(
        min_value=0, above=0, allow_null=True, required=False, default=None
    )
    export_limit_mw = FiniteFloat(
        min_value=0, above=0, allow_null=True, required=False, default=None
    )
    measurement_basis = serializers.ChoiceField(
        choices=MeasurementBasis.choices, default=MeasurementBasis.NET_EXPORT
    )
    mounting_type = serializers.ChoiceField(
        choices=MountingType.choices, default=MountingType.UNKNOWN
    )
    surface_tilt_degrees = FiniteFloat(
        min_value=0, max_value=TILT_MAX_DEGREES, allow_null=True, required=False, default=None
    )
    surface_azimuth_degrees = FiniteFloat(
        min_value=0, below=AZIMUTH_LIMIT_DEGREES, allow_null=True, required=False, default=None
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        angles = (
            attrs.get("surface_tilt_degrees") is not None
            or attrs.get("surface_azimuth_degrees") is not None
        )
        if angles and attrs.get("mounting_type") != "fixed":
            raise serializers.ValidationError(
                {"mounting_type": [gettext("Enter panel angles only for fixed mounting.")]}
            )
        return attrs

    def parameters(self) -> PlantParameters:
        return PlantParameters(
            **{
                field.name: self.validated_data[field.name]
                for field in dataclass_fields(PlantParameters)
            }
        )


class PlantRevisionInput(PlantInput):
    expected_revision = serializers.IntegerField(
        min_value=1, help_text="The parameters_revision the change was made from."
    )


class PlantParametersOutput(serializers.Serializer[PlantSubmission | PlantConfiguration]):
    dc_capacity_mw = serializers.FloatField()
    inverter_capacity_mw = serializers.FloatField(allow_null=True)
    export_limit_mw = serializers.FloatField(allow_null=True)
    measurement_basis = serializers.ChoiceField(choices=MeasurementBasis.choices)
    mounting_type = serializers.ChoiceField(choices=MountingType.choices)
    surface_tilt_degrees = serializers.FloatField(allow_null=True)
    surface_azimuth_degrees = serializers.FloatField(allow_null=True)


class ConfigurationOutput(serializers.ModelSerializer[PlantConfiguration]):
    class Meta:
        model = PlantConfiguration
        fields = (
            "id",
            "revision",
            "dc_capacity_mw",
            "inverter_capacity_mw",
            "export_limit_mw",
            "measurement_basis",
            "mounting_type",
            "surface_tilt_degrees",
            "surface_azimuth_degrees",
            "created_at",
        )
        read_only_fields = fields


class PlantOutput(serializers.ModelSerializer[Plant]):
    region_id = serializers.IntegerField()
    current_configuration = ConfigurationOutput(allow_null=True)
    parameters = serializers.SerializerMethodField(
        help_text="The declared values: those awaiting approval, else the current configuration."
    )

    @extend_schema_field(PlantParametersOutput(allow_null=True))
    def get_parameters(self, plant: Plant) -> dict[str, Any] | None:
        record = declaration(plant)
        return None if record is None else dict(PlantParametersOutput(record).data)

    class Meta:
        model = Plant
        fields = (
            "id",
            "name",
            "region_id",
            "current_configuration",
            "parameters",
            "parameters_revision",
            "created_at",
        )
        read_only_fields = fields


class PlantConflict(serializers.Serializer[dict[str, Any]]):
    code = serializers.ChoiceField(choices=ErrorCode.choices)
    message = serializers.CharField()
    current = PlantOutput()


class ObservationRow(InputSerializer):
    starts_at = AwareDateTime()
    ends_at = AwareDateTime()
    energy = FiniteFloat(min_value=0)
    quality = serializers.ChoiceField(
        choices=ObservationQuality.choices, default=ObservationQuality.VALID
    )
    source = serializers.CharField(max_length=200)  # type: ignore[assignment]


class ObservationImport(InputSerializer):
    configuration_id = serializers.IntegerField(min_value=1)
    unit = serializers.ChoiceField(choices=("MWh", "kWh"))
    observations: Any = serializers.ListSerializer(
        child=ObservationRow(), allow_empty=False, max_length=PRODUCTION_BATCH_LIMIT
    )


class ObservationImportOutput(serializers.Serializer[dict[str, Any]]):
    created = serializers.IntegerField()
    existing = serializers.IntegerField()
    count = serializers.IntegerField()
    ids = serializers.ListField(child=serializers.IntegerField())


class ProductionChangeRow(InputSerializer):
    starts_at = AwareDateTime()
    ends_at = AwareDateTime()
    expected_revision_id = serializers.IntegerField(min_value=1, allow_null=True)
    action = serializers.ChoiceField(choices=ProductionChangeAction.choices)
    energy = FiniteFloat(min_value=0, required=False)
    quality = serializers.ChoiceField(
        choices=ObservationQuality.choices, default=ObservationQuality.VALID
    )
    source = serializers.CharField(max_length=200, default="operator")  # type: ignore[assignment]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["action"] == ProductionChangeAction.SET and "energy" not in attrs:
            raise serializers.ValidationError({"energy": gettext("Enter a value.")})
        return attrs


class ProductionChangesInput(InputSerializer):
    configuration_id = serializers.IntegerField(min_value=1)
    unit = serializers.ChoiceField(choices=("MWh", "kWh"))
    changes: Any = serializers.ListSerializer(
        child=ProductionChangeRow(), allow_empty=False, max_length=PRODUCTION_BATCH_LIMIT
    )


class ProductionCommitInput(ProductionChangesInput):
    origin = serializers.ChoiceField(choices=("editor", "csv_import"), required=False)
    file_name = serializers.CharField(max_length=255, required=False)
    file_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$", required=False)
    confirm_implausible = serializers.BooleanField(default=False)


class ProductionPreviewRowOutput(serializers.Serializer[dict[str, Any]]):
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    current_energy_mwh = serializers.FloatField(allow_null=True)
    new_energy_mwh = serializers.FloatField()
    corrected = serializers.BooleanField()


class ProductionPreviewWarningOutput(serializers.Serializer[dict[str, Any]]):
    code = serializers.ChoiceField(choices=("provider_hour_incomplete", "energy_exceeds_capacity"))
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()


class ProductionPreviewOutput(serializers.Serializer[dict[str, Any]]):
    changes = ProductionChangeRow(many=True)
    preview = ProductionPreviewRowOutput(many=True)
    warnings = ProductionPreviewWarningOutput(many=True)


class ObservationOutput(serializers.ModelSerializer[ProductionObservation]):
    configuration_id = serializers.IntegerField()
    recorded_by_id = serializers.IntegerField()
    supersedes_id = serializers.IntegerField(allow_null=True)

    class Meta:
        model = ProductionObservation
        fields = (
            "id",
            "configuration_id",
            "starts_at",
            "ends_at",
            "energy_mwh",
            "quality",
            "source",
            "recorded_by_id",
            "created_at",
            "supersedes_id",
            "is_reverted",
        )
        read_only_fields = fields


class CalibrationInput(InputSerializer):
    configuration_id = serializers.IntegerField(min_value=1)
    start_date = serializers.DateField()
    end_date = serializers.DateField()


class ProductionQuery(InputSerializer):
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    resolution_minutes = serializers.ChoiceField(choices=(30, 60), default=60)
    daylight_only = serializers.BooleanField(default=False)
    native_intervals = serializers.BooleanField(default=False)


class PerformanceQuery(InputSerializer):
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    daylight_only = serializers.BooleanField(default=False)
    comparison = serializers.ChoiceField(
        choices=("archived_forecast", "historical_model"), default="archived_forecast"
    )
    issuance_cutoff = serializers.TimeField(
        required=False, allow_null=True, input_formats=("%H:%M", "%H:%M:%S", "%H:%M:%S.%f")
    )


class PerformanceMetrics(serializers.Serializer[dict[str, Any]]):
    interval_mae_mw = serializers.FloatField()
    source_positive_mae_mw = serializers.FloatField(allow_null=True)
    bias_mw = serializers.FloatField()
    daily_mae_mwh = serializers.FloatField()
    daily_bias_mwh = serializers.FloatField()


class PerformanceDay(serializers.Serializer[dict[str, Any]]):
    date = serializers.DateField()
    status = serializers.ChoiceField(choices=("matched", "missing"))
    reason = serializers.ChoiceField(choices=DayReason.choices, allow_null=True)
    selection_policy = serializers.ChoiceField(choices=SelectionPolicy.choices, allow_null=True)
    snapshot_id = serializers.IntegerField(allow_null=True)
    issuance_id = serializers.IntegerField(allow_null=True)
    configuration_id = serializers.IntegerField(allow_null=True)
    model_version = serializers.CharField(allow_null=True)
    source_snapshot_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    reference_provenance = serializers.JSONField(required=False)
    collected_at = serializers.DateTimeField(allow_null=True)
    available_at = serializers.DateTimeField(allow_null=True)
    selection_cutoff = serializers.DateTimeField(allow_null=True)
    calibration_id = serializers.IntegerField(allow_null=True)
    calibration_in_sample = serializers.BooleanField(allow_null=True)
    factor = serializers.FloatField(allow_null=True)
    measured_energy_mwh = serializers.FloatField(allow_null=True)
    predicted_energy_mwh = serializers.FloatField(allow_null=True)
    baseline_energy_mwh = serializers.FloatField(allow_null=True)
    error_mwh = serializers.FloatField(allow_null=True)
    manual_observation_ids = serializers.ListField(child=serializers.IntegerField())
    settlement_snapshot_ids = serializers.ListField(child=serializers.IntegerField())
    provisional_snapshot_ids = serializers.ListField(child=serializers.IntegerField())
    source_kind = serializers.CharField(allow_null=True)
    historical_configuration_assumed = serializers.BooleanField()
    provisional_production = serializers.BooleanField()


class PerformanceOutput(serializers.Serializer[dict[str, Any]]):
    plant_id = serializers.IntegerField()
    configuration_id = serializers.IntegerField(allow_null=True)
    timezone = serializers.CharField()
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    evaluated_at = serializers.DateTimeField()
    selection_policy = serializers.ChoiceField(choices=SelectionPolicy.choices)
    comparison = serializers.ChoiceField(choices=("archived_forecast", "historical_model"))
    issuance_cutoff = serializers.TimeField(allow_null=True)
    measurement_policy = serializers.CharField()
    configuration_created_at = serializers.DateTimeField(allow_null=True)
    measurement_basis = serializers.CharField(allow_null=True)
    metric_scope = serializers.ChoiceField(choices=("daylight_intervals", "full_day"))
    daylight_policy = serializers.CharField(allow_null=True)
    matched_days = serializers.IntegerField()
    requested_days = serializers.IntegerField()
    missing_days = serializers.IntegerField()
    excluded_days = serializers.IntegerField()
    scored_intervals = serializers.IntegerField()
    scored_hours = serializers.FloatField()
    relative_error = serializers.FloatField(
        allow_null=True, help_text="Sum of absolute daily errors over measured energy."
    )
    skill_score = serializers.FloatField(
        allow_null=True,
        help_text="One minus the absolute daily error over that of the uncalibrated reference, "
        "on days the calibration did not train on.",
    )
    skill_days = serializers.IntegerField()
    metrics = PerformanceMetrics(allow_null=True)
    baseline_metrics = PerformanceMetrics(allow_null=True)
    days = PerformanceDay(many=True)
    uncertainty = serializers.CharField()


class DaylightWindowOutput(serializers.Serializer[dict[str, Any]]):
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()


class ProductionDayOutput(serializers.Serializer[dict[str, Any]]):
    date = serializers.DateField()
    available_count = serializers.IntegerField()
    recorded_count = serializers.IntegerField()
    missing_count = serializers.IntegerField()
    sunrise = serializers.DateTimeField(allow_null=True)
    sunset = serializers.DateTimeField(allow_null=True)
    daylight_status = serializers.ChoiceField(choices=("normal", "polar_day", "polar_night"))
    daylight_windows = DaylightWindowOutput(many=True)
    total_mwh = serializers.FloatField(allow_null=True)
    complete = serializers.BooleanField()


class ProductionValueOutput(serializers.Serializer[dict[str, Any]]):
    energy_mwh = serializers.FloatField()
    quality = serializers.CharField()
    source = serializers.CharField()  # type: ignore[assignment]
    source_kind = serializers.CharField()
    provisional = serializers.BooleanField()
    aggregated = serializers.BooleanField()


class ProductionProviderOutput(serializers.Serializer[dict[str, Any]]):
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    energy_mwh = serializers.FloatField()
    source_kind = serializers.CharField()
    snapshot_id = serializers.IntegerField()
    collected_at = serializers.DateTimeField()
    available_at = serializers.DateTimeField()
    content_sha256 = serializers.CharField()
    provisional = serializers.BooleanField()


class EarlierConfigurationObservationOutput(ObservationOutput):
    configuration_revision = serializers.IntegerField()

    class Meta:
        model = ProductionObservation
        fields: tuple[str, ...] = (*ObservationOutput.Meta.fields, "configuration_revision")
        read_only_fields = fields


class ProductionIntervalOutput(serializers.Serializer[dict[str, Any]]):
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    observation = ObservationOutput(allow_null=True)
    previous_configuration_observations = EarlierConfigurationObservationOutput(many=True)
    revision_id = serializers.IntegerField(allow_null=True)
    effective = ProductionValueOutput(allow_null=True)
    provider_originals = ProductionProviderOutput(many=True)
    can_edit = serializers.BooleanField()
    can_revert = serializers.BooleanField()
    edit_unavailable_reason = serializers.CharField(allow_null=True)
    can_propose = serializers.BooleanField()
    can_propose_revert = serializers.BooleanField()
    proposal_unavailable_reason = serializers.CharField(allow_null=True)
    pending_request_id = serializers.IntegerField(allow_null=True)


class ProductionHistoryOutput(serializers.Serializer[dict[str, Any]]):
    plant_id = serializers.IntegerField()
    configuration_id = serializers.IntegerField(allow_null=True)
    timezone = serializers.CharField()
    available_start_date = serializers.DateField(allow_null=True)
    available_end_date = serializers.DateField(allow_null=True)
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    resolution_minutes = serializers.ChoiceField(choices=(30, 60))
    native_intervals = serializers.BooleanField()
    slot_scope = serializers.ChoiceField(choices=("daylight", "full_day"))
    daylight_policy = serializers.CharField()
    days = ProductionDayOutput(many=True)
    intervals = ProductionIntervalOutput(many=True)


class CalibrationSummaryOutput(serializers.ModelSerializer[CalibrationRun]):
    configuration_id = serializers.IntegerField()
    binding_id = serializers.IntegerField()
    requested_by_id = serializers.IntegerField()
    assessment = serializers.ChoiceField(
        choices=CalibrationAssessment.choices, allow_blank=True, read_only=True
    )
    reason = serializers.SerializerMethodField()
    selection_state = serializers.SerializerMethodField()

    @extend_schema_field(serializers.ChoiceField(choices=CalibrationError.choices, allow_null=True))
    def get_reason(self, run: CalibrationRun) -> str | None:
        return run.last_error or None

    @extend_schema_field(serializers.ChoiceField(choices=SelectionState.choices))
    def get_selection_state(self, run: CalibrationRun) -> str:
        return selection_state(run, self.context["selection"])

    class Meta:
        model = CalibrationRun
        fields = (
            "id",
            "configuration_id",
            "binding_id",
            "requested_by_id",
            "window_start",
            "window_end",
            "cutoff_at",
            "created_at",
            "completed_at",
            "status",
            "reason",
            "selection_state",
            "assessment",
            "factor",
            "result",
            "dataset_sha256",
        )
        read_only_fields = fields


class CalibrationOutput(CalibrationSummaryOutput):
    activation_blocker = serializers.SerializerMethodField()

    @extend_schema_field(
        serializers.ChoiceField(choices=ActivationBlocker.choices, allow_null=True)
    )
    def get_activation_blocker(self, run: CalibrationRun) -> str | None:
        blocker: str | None = self.context["blocker"]
        return blocker

    class Meta:
        model = CalibrationRun
        fields = (*CalibrationSummaryOutput.Meta.fields, "activation_blocker", "dataset_manifest")
        read_only_fields = fields


class ActivationOutput(serializers.ModelSerializer[CalibrationActivation]):
    configuration_id = serializers.IntegerField()
    calibration_id = serializers.IntegerField(allow_null=True)
    actor_id = serializers.IntegerField(allow_null=True)

    class Meta:
        model = CalibrationActivation
        fields = ("id", "configuration_id", "calibration_id", "actor_id", "created_at")
        read_only_fields = fields


class EmptyInput(InputSerializer):
    pass


class CalibrationAutomationInput(InputSerializer):
    enabled = serializers.BooleanField()


class AutomaticCalibrationAttemptOutput(serializers.Serializer[dict[str, Any]]):
    calibration_id = serializers.IntegerField(allow_null=True)
    status = serializers.ChoiceField(choices=("activated", "skipped"))
    reason = serializers.CharField()
    created_at = serializers.DateTimeField()


class CurrentCalibrationOutput(serializers.Serializer[dict[str, Any]]):
    activation_id = serializers.IntegerField()
    calibration_id = serializers.IntegerField(allow_null=True)
    factor = serializers.FloatField(allow_null=True)
    actor_id = serializers.IntegerField(allow_null=True)
    origin = serializers.ChoiceField(choices=("manual", "automatic"))
    created_at = serializers.DateTimeField()
    effective = serializers.BooleanField()


class CalibrationAutomationOutput(serializers.Serializer[dict[str, Any]]):
    enabled = serializers.BooleanField()
    authorized = serializers.BooleanField()
    configuration_id = serializers.IntegerField(allow_null=True)
    configuration_current = serializers.BooleanField()
    next_check_at = serializers.DateTimeField(allow_null=True)
    disabled_cause = serializers.ChoiceField(choices=AutomationCause.choices, allow_null=True)
    current_calibration = CurrentCalibrationOutput(allow_null=True)
    last_attempt = AutomaticCalibrationAttemptOutput(allow_null=True)


class ForecastQuery(InputSerializer):
    date = serializers.DateField()
    data_source = serializers.ChoiceField(
        choices=("auto", "forecast", "estimated_actuals", "archived_forecast"), default="auto"
    )
    resolution_minutes = serializers.ChoiceField(choices=(30, 60), default=30)
    as_of = AwareDateTime(required=False)


class PowerIntervalOutput(serializers.Serializer[dict[str, Any]]):
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    mean_power_mw = serializers.FloatField(allow_null=True, required=False)
    energy_mwh = serializers.FloatField(allow_null=True, required=False)
    partial_energy_mwh = serializers.FloatField(allow_null=True, required=False)
    coverage_fraction = serializers.FloatField(allow_null=True, required=False)
    complete = serializers.BooleanField(required=False)
    quality_flags = serializers.ListField(child=serializers.CharField(), required=False)
    source_snapshot_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    source_kinds = serializers.ListField(child=serializers.CharField(), required=False)


class PlantResultConfigurationOutput(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    revision = serializers.IntegerField()
    dc_capacity_mw = serializers.FloatField(allow_null=True)
    inverter_capacity_mw = serializers.FloatField(allow_null=True)
    export_limit_mw = serializers.FloatField(allow_null=True)
    measurement_basis = serializers.CharField()


class PlantForecastResultOutput(serializers.Serializer[dict[str, Any]]):
    """Energy in MWh and mean power in MW on the measurement basis of the plant."""

    start = serializers.DateTimeField(required=False)
    end = serializers.DateTimeField(required=False)
    resolution_minutes = serializers.IntegerField(required=False)
    limit_mw = serializers.FloatField(allow_null=True, required=False)
    factor = serializers.FloatField(required=False)
    energy_mwh = serializers.FloatField(allow_null=True, required=False)
    partial_energy_mwh = serializers.FloatField(allow_null=True, required=False)
    coverage_fraction = serializers.FloatField(allow_null=True, required=False)
    complete = serializers.BooleanField(required=False)
    model_version = serializers.CharField(required=False)
    quality_flags = serializers.ListField(child=serializers.CharField(), required=False)
    daylight = DaylightOutput(required=False)
    intervals = PowerIntervalOutput(many=True, required=False)
    configuration = PlantResultConfigurationOutput(required=False)
    provenance = ForecastProvenanceOutput(required=False)


class OutageScenarioOutput(serializers.Serializer[dict[str, Any]]):
    evaluated_at = serializers.DateTimeField()
    status = serializers.ChoiceField(choices=("estimated", "unknown_impact"))
    incident_revision_ids = serializers.ListField(child=serializers.IntegerField())
    incidents = serializers.ListField(child=serializers.DictField())
    result = PlantForecastResultOutput()


class DiagnosticDayOutput(serializers.Serializer[dict[str, Any]]):
    day = serializers.DateField()
    performance_index = serializers.FloatField()
    hours = serializers.FloatField()
    status = serializers.ChoiceField(choices=("shortfall", "within_threshold"))
    observed_zero = serializers.BooleanField()


class ProductionDiagnosticOutput(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=("suspected", "normal", "insufficient"))
    performance_ratio = serializers.FloatField(allow_null=True)
    reference_kind = serializers.ChoiceField(
        choices=("active_calibration", "uncalibrated_reference")
    )
    window_start = serializers.DateField(allow_null=True)
    window_end = serializers.DateField(allow_null=True)
    days = DiagnosticDayOutput(many=True)
    latest_matched_day = serializers.DateField(allow_null=True)
    latest_day_status = serializers.ChoiceField(
        choices=("shortfall", "within_threshold", "insufficient", "unavailable")
    )
    latest_performance_index = serializers.FloatField(allow_null=True)
    evidence_state = serializers.ChoiceField(choices=("current", "stale"))


@extend_schema_field(PlantForecastResultOutput(allow_null=True))
class PlantForecastResultField(serializers.JSONField):
    pass


@extend_schema_field(IrradianceSummaryOutput(allow_null=True))
class IrradianceSummaryField(serializers.JSONField):
    pass


@extend_schema_field(OutageScenarioOutput(allow_null=True))
class OutageScenarioField(serializers.JSONField):
    pass


@extend_schema_field(ProductionDiagnosticOutput(allow_null=True))
class ProductionDiagnosticField(serializers.JSONField):
    pass


class PlantForecastOutput(ForecastEnvelope):
    plant_id = serializers.IntegerField()
    as_of = serializers.DateTimeField()
    configuration_id = serializers.IntegerField(allow_null=True)
    calibration_id = serializers.IntegerField(allow_null=True)
    issuance_id = serializers.IntegerField(allow_null=True, required=False)
    snapshot_id = serializers.IntegerField(allow_null=True, required=False)
    result = PlantForecastResultField(allow_null=True)
    regional_irradiance = IrradianceSummaryField(allow_null=True)
    outage_scenario = OutageScenarioField(allow_null=True, default=None)
    production_diagnostic = ProductionDiagnosticField(allow_null=True, default=None)


ROWS_PER_DAY_LIMIT = 52


class ForecastDayRowOutput(serializers.Serializer[Any]):
    """Energy in MWh on the measurement basis of the plant."""

    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    mwh = serializers.FloatField(allow_null=True)
    affected_mwh = serializers.FloatField(allow_null=True)
    sources = serializers.ListField(child=serializers.CharField(), allow_null=True)


class AffectedDayOutput(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=("estimated", "unknown_impact"))
    total_mwh = serializers.FloatField(allow_null=True)

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        return {**super().get_fields(), "partial": serializers.BooleanField()}


class DayUnavailableReason(models.TextChoices):
    CONFIGURATION_UNAVAILABLE = "configuration_unavailable"
    FORECAST_UNAVAILABLE = "forecast_unavailable"
    ESTIMATED_ACTUALS_UNAVAILABLE = "estimated_actuals_unavailable"
    CALCULATION_OUT_OF_RANGE = ErrorCode.CALCULATION_OUT_OF_RANGE.value


class ForecastDayOutput(ForecastEnvelope):
    unavailable_reason = serializers.ChoiceField(
        choices=DayUnavailableReason.choices, allow_null=True
    )
    collected_at = serializers.DateTimeField(allow_null=True)
    total_mwh = serializers.FloatField(allow_null=True)

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        return {
            **super().get_fields(),
            "partial": serializers.BooleanField(),
            "affected": AffectedDayOutput(allow_null=True),
            "rows": serializers.ListField(
                child=ForecastDayRowOutput(), max_length=ROWS_PER_DAY_LIMIT
            ),
        }


class ProductionDayRowOutput(serializers.Serializer[Any]):
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    mwh = serializers.FloatField(allow_null=True)
    quality = serializers.CharField(allow_null=True)

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        return {
            **super().get_fields(),
            "source": serializers.CharField(allow_null=True),
            "provisional": serializers.BooleanField(allow_null=True),
        }


class SavedProductionDayOutput(serializers.Serializer[Any]):
    date = serializers.DateField()
    timezone = serializers.CharField()
    total_mwh = serializers.FloatField(allow_null=True)
    complete = serializers.BooleanField()
    rows = serializers.ListField(child=ProductionDayRowOutput(), max_length=ROWS_PER_DAY_LIMIT)


class PlantDaysOutput(serializers.Serializer[Any]):
    plant_id = serializers.IntegerField()
    name = serializers.CharField()
    today = serializers.DateField()
    forecast = ForecastDayOutput(many=True)
    production = SavedProductionDayOutput(many=True)
