from typing import Any

from django.utils.dateparse import parse_datetime
from rest_framework import serializers

from pyracast.api.serializers import InputSerializer
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
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
            raise serializers.ValidationError("A timezone-aware ISO 8601 timestamp is required.")
        return super().to_internal_value(value)


class PlantInput(InputSerializer):
    name = serializers.CharField(max_length=200)
    dc_capacity_mw = FiniteFloat(min_value=0)
    inverter_capacity_mw = FiniteFloat(min_value=0, allow_null=True, required=False, default=None)
    export_limit_mw = FiniteFloat(min_value=0, allow_null=True, required=False, default=None)
    measurement_basis = serializers.ChoiceField(
        choices=("net_export", "ac_generation"), default="net_export"
    )
    mounting_type = serializers.ChoiceField(
        choices=("unknown", "fixed", "tracking"), default="unknown"
    )
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


class PlantOutput(serializers.ModelSerializer[Plant]):
    region_id = serializers.IntegerField()
    current_configuration = ConfigurationOutput(allow_null=True)

    class Meta:
        model = Plant
        fields = ("id", "name", "region_id", "current_configuration", "created_at")
        read_only_fields = fields


class ObservationRow(InputSerializer):
    starts_at = AwareDateTime()
    ends_at = AwareDateTime()
    energy = FiniteFloat(min_value=0)
    quality = serializers.ChoiceField(
        choices=("valid", "outage", "curtailed", "invalid"), default="valid"
    )
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


class ObservationOutput(serializers.ModelSerializer[ProductionObservation]):
    configuration_id = serializers.IntegerField()
    recorded_by_id = serializers.IntegerField()

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


class PerformanceQuery(InputSerializer):
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    daylight_only = serializers.BooleanField(default=False)
    comparison = serializers.ChoiceField(
        choices=("archived_forecast", "historical_model"), default="archived_forecast"
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
    collected_at = serializers.DateTimeField(allow_null=True)
    available_at = serializers.DateTimeField(allow_null=True)
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
    measurement_policy = serializers.CharField()
    configuration_created_at = serializers.DateTimeField(allow_null=True)
    measurement_basis = serializers.CharField(allow_null=True)
    metric_scope = serializers.ChoiceField(choices=("daylight_intervals", "full_day"))
    daylight_policy = serializers.CharField(allow_null=True)
    matched_days = serializers.IntegerField()
    requested_days = serializers.IntegerField()
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


class ProductionIntervalOutput(serializers.Serializer[dict[str, Any]]):
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    reference_available = serializers.BooleanField()
    observation = ObservationOutput(allow_null=True)
    can_enter = serializers.BooleanField()
    unavailable_reason = serializers.CharField(allow_null=True)


class ProductionHistoryOutput(serializers.Serializer[dict[str, Any]]):
    plant_id = serializers.IntegerField()
    configuration_id = serializers.IntegerField(allow_null=True)
    timezone = serializers.CharField()
    available_start_date = serializers.DateField(allow_null=True)
    available_end_date = serializers.DateField(allow_null=True)
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    resolution_minutes = serializers.ChoiceField(choices=(30, 60))
    slot_scope = serializers.ChoiceField(choices=("daylight", "full_day"))
    daylight_policy = serializers.CharField()
    days = ProductionDayOutput(many=True)
    intervals = ProductionIntervalOutput(many=True)
    observations = ObservationOutput(many=True)


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
    actor_id = serializers.IntegerField()

    class Meta:
        model = CalibrationActivation
        fields = ("id", "configuration_id", "calibration_id", "actor_id", "created_at")
        read_only_fields = fields


class EmptyInput(InputSerializer):
    pass


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
    freshness = serializers.CharField()
    source_kind = serializers.ChoiceField(choices=("forecast", "estimated_actuals"))
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


def calibration_status_choices() -> Any:
    return CalibrationRun._meta.get_field("status").choices
