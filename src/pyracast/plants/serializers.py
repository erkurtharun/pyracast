from typing import Any

from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext
from rest_framework import serializers

from pyracast.api.serializers import InputSerializer
from pyracast.change_requests.models import ProductionChangeAction
from pyracast.plants.models import (
    MEASUREMENT_BASES,
    MOUNTING_TYPES,
    OBSERVATION_QUALITIES,
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantSubmission,
    ProductionObservation,
)


class FiniteFloat(serializers.FloatField):
    def to_internal_value(self, data: Any) -> float:
        if isinstance(data, bool):
            self.fail("invalid")
        return super().to_internal_value(data)


class AwareDateTime(serializers.DateTimeField):
    def to_internal_value(self, value: Any) -> Any:
        if not isinstance(value, str):
            self.fail("invalid", format="ISO 8601")
        try:
            parsed = parse_datetime(value)
        except ValueError:
            parsed = None
        if parsed is None or parsed.utcoffset() is None:
            raise serializers.ValidationError(
                gettext("A timezone-aware ISO 8601 timestamp is required.")
            )
        return super().to_internal_value(value)


class PlantInput(InputSerializer):
    name = serializers.CharField(max_length=200)
    dc_capacity_mw = FiniteFloat(min_value=0)
    inverter_capacity_mw = FiniteFloat(min_value=0, allow_null=True, required=False, default=None)
    export_limit_mw = FiniteFloat(min_value=0, allow_null=True, required=False, default=None)
    measurement_basis = serializers.ChoiceField(choices=MEASUREMENT_BASES, default="net_export")
    mounting_type = serializers.ChoiceField(choices=MOUNTING_TYPES, default="unknown")
    surface_tilt = FiniteFloat(
        min_value=0, max_value=90, allow_null=True, required=False, default=None
    )
    surface_azimuth = FiniteFloat(min_value=0, allow_null=True, required=False, default=None)


class ConfigurationOutput(serializers.ModelSerializer[PlantConfiguration]):
    binding_id = serializers.IntegerField()

    class Meta:
        model = PlantConfiguration
        fields = (
            "id",
            "binding_id",
            "revision",
            "dc_capacity_mw",
            "inverter_capacity_mw",
            "export_limit_mw",
            "measurement_basis",
            "mounting_type",
            "surface_tilt",
            "surface_azimuth",
            "created_at",
        )
        read_only_fields = fields


class PlantSubmissionOutput(serializers.ModelSerializer[PlantSubmission]):
    class Meta:
        model = PlantSubmission
        fields = (
            "id",
            "dc_capacity_mw",
            "inverter_capacity_mw",
            "export_limit_mw",
            "measurement_basis",
            "mounting_type",
            "surface_tilt",
            "surface_azimuth",
            "created_at",
        )
        read_only_fields = fields


class PlantOutput(serializers.ModelSerializer[Plant]):
    region_id = serializers.IntegerField()
    current_configuration = ConfigurationOutput(allow_null=True)
    submission = PlantSubmissionOutput(allow_null=True)

    class Meta:
        model = Plant
        fields = ("id", "name", "region_id", "current_configuration", "submission", "created_at")
        read_only_fields = fields


class ObservationRow(InputSerializer):
    starts_at = AwareDateTime()
    ends_at = AwareDateTime()
    energy = FiniteFloat(min_value=0)
    quality = serializers.ChoiceField(choices=OBSERVATION_QUALITIES, default="valid")
    source = serializers.CharField(max_length=200)  # type: ignore[assignment]


class ObservationImport(InputSerializer):
    configuration_id = serializers.IntegerField(min_value=1)
    unit = serializers.ChoiceField(choices=("MWh", "kWh"))
    observations: Any = serializers.ListSerializer(
        child=ObservationRow(), allow_empty=False, max_length=2000
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
    quality = serializers.ChoiceField(choices=OBSERVATION_QUALITIES, default="valid")
    source = serializers.CharField(max_length=200, default="operator")  # type: ignore[assignment]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["action"] == "set" and "energy" not in attrs:
            raise serializers.ValidationError({"energy": gettext("This field is required.")})
        return attrs


class ProductionChangesInput(InputSerializer):
    configuration_id = serializers.IntegerField(min_value=1)
    unit = serializers.ChoiceField(choices=("MWh", "kWh"))
    changes: Any = serializers.ListSerializer(
        child=ProductionChangeRow(), allow_empty=False, max_length=2000
    )


class ProductionPreviewRowOutput(serializers.Serializer[dict[str, Any]]):
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    current_energy_mwh = serializers.FloatField(allow_null=True)
    new_energy_mwh = serializers.FloatField()
    corrected = serializers.BooleanField()


class ProductionPreviewWarningOutput(serializers.Serializer[dict[str, Any]]):
    code = serializers.CharField()
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
            "recorded_at",
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
    reason = serializers.CharField(allow_null=True)
    snapshot_id = serializers.IntegerField(allow_null=True)
    issuance_id = serializers.IntegerField(allow_null=True, required=False)
    configuration_id = serializers.IntegerField(allow_null=True, required=False)
    model_version = serializers.CharField(allow_null=True, required=False)
    source_snapshot_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    reference_provenance = serializers.JSONField(required=False)
    collected_at = serializers.DateTimeField(allow_null=True)
    available_at = serializers.DateTimeField(allow_null=True)
    selection_cutoff = serializers.DateTimeField(allow_null=True)
    calibration_id = serializers.IntegerField(allow_null=True)
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
    evaluation_kind = serializers.CharField()
    selection_policy = serializers.CharField()
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


class ProductionIntervalOutput(serializers.Serializer[dict[str, Any]]):
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    reference_available = serializers.BooleanField()
    observation = ObservationOutput(allow_null=True)
    can_enter = serializers.BooleanField()
    unavailable_reason = serializers.CharField(allow_null=True)
    revision_id = serializers.IntegerField(allow_null=True)
    effective = ProductionValueOutput(allow_null=True)
    provider_original = ProductionProviderOutput(allow_null=True)
    provider_originals = ProductionProviderOutput(many=True)
    can_edit = serializers.BooleanField()
    can_revert = serializers.BooleanField()
    edit_unavailable_reason = serializers.CharField(allow_null=True)
    can_propose = serializers.BooleanField()
    can_propose_revert = serializers.BooleanField()
    proposal_unavailable_reason = serializers.CharField(allow_null=True)


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
    observations = ObservationOutput(many=True)
    revisions = ObservationOutput(many=True)


class CalibrationOutput(serializers.ModelSerializer[CalibrationRun]):
    configuration_id = serializers.IntegerField()
    binding_id = serializers.IntegerField()
    requested_by_id = serializers.IntegerField()

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
            "assessment",
            "factor",
            "result",
            "dataset_manifest",
            "dataset_sha256",
        )
        read_only_fields = fields


class CalibrationSummaryOutput(CalibrationOutput):
    class Meta:
        model = CalibrationRun
        fields = tuple(
            field for field in CalibrationOutput.Meta.fields if field != "dataset_manifest"
        )
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


class CalibrationAutomationOutput(serializers.Serializer[dict[str, Any]]):
    enabled = serializers.BooleanField()
    authorized = serializers.BooleanField()
    configuration_id = serializers.IntegerField(allow_null=True)
    next_check_at = serializers.DateTimeField(allow_null=True)
    last_attempt = AutomaticCalibrationAttemptOutput(allow_null=True)


class ForecastQuery(InputSerializer):
    date = serializers.DateField()
    data_source = serializers.ChoiceField(
        choices=("auto", "forecast", "estimated_actuals", "archived_forecast"), default="forecast"
    )
    resolution_minutes = serializers.ChoiceField(choices=(30, 60), default=30)
    as_of = AwareDateTime(required=False)


class PlantForecastOutput(serializers.Serializer[dict[str, Any]]):
    plant_id = serializers.IntegerField()
    date = serializers.DateField()
    timezone = serializers.CharField()
    as_of = serializers.DateTimeField()
    configuration_id = serializers.IntegerField(allow_null=True)
    calibration_id = serializers.IntegerField(allow_null=True)
    issuance_id = serializers.IntegerField(allow_null=True, required=False)
    freshness = serializers.CharField()
    source_kind = serializers.ChoiceField(choices=("forecast", "estimated_actuals", "mixed"))
    unavailable_reason = serializers.ChoiceField(
        choices=(
            "configuration_unavailable",
            "forecast_unavailable",
            "estimated_actuals_unavailable",
        ),
        allow_null=True,
    )
    result = serializers.JSONField(allow_null=True)
    regional_irradiance = serializers.JSONField(allow_null=True)
    outage_scenario = serializers.JSONField(allow_null=True, default=None)
    production_diagnostic = serializers.JSONField(allow_null=True, default=None)


def calibration_status_choices() -> Any:
    return CalibrationRun._meta.get_field("status").choices
