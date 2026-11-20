from typing import Any, cast

from django.db import models
from django.utils.translation import gettext
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from pyracast.accounts.models import Tenant, WorkspaceRole
from pyracast.forecasts.models import SnapshotSourceKind
from pyracast.regions.models import (
    Region,
    RegionLocationChangeRequest,
)


class InputSerializer(serializers.Serializer[dict[str, Any]]):
    def run_validation(self, data: Any = serializers.empty) -> Any:
        if data is None:
            # DRF's own refusal of a null body is an untranslated literal.
            raise serializers.ValidationError(
                {"non_field_errors": [gettext("Send the request body as a JSON object.")]}
            )
        return super().run_validation(data)

    def to_internal_value(self, data: Any) -> dict[str, Any]:
        if isinstance(data, dict) and data.keys() - self.fields.keys():
            raise serializers.ValidationError(
                {"non_field_errors": [gettext("Unknown fields are not accepted.")]}
            )
        return cast(dict[str, Any], super().to_internal_value(data))


class FiniteFloat(serializers.FloatField):
    def __init__(
        self, *, above: float | None = None, below: float | None = None, **kwargs: Any
    ) -> None:
        self.above, self.below = above, below
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> float:
        if isinstance(data, bool):
            self.fail("invalid")
        value = super().to_internal_value(data)
        if self.above is not None and value <= self.above:
            raise serializers.ValidationError(
                gettext("Enter a number greater than %(limit)s.") % {"limit": self.above}
            )
        if self.below is not None and value >= self.below:
            raise serializers.ValidationError(
                gettext("Enter a number less than %(limit)s.") % {"limit": self.below}
            )
        return value


class TenantOutput(serializers.ModelSerializer[Tenant]):
    role = serializers.ChoiceField(choices=WorkspaceRole.choices, read_only=True)
    pending_review_count = serializers.IntegerField(allow_null=True, read_only=True)
    unread_outcome_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Tenant
        fields = ("id", "name", "role", "pending_review_count", "unread_outcome_count")
        read_only_fields = fields


class RegionInput(InputSerializer):
    name = serializers.CharField(max_length=200)
    latitude = FiniteFloat(min_value=-90, max_value=90)
    longitude = FiniteFloat(min_value=-180, max_value=180)
    timezone = serializers.CharField(max_length=100)


class RegionOutput(serializers.ModelSerializer[Region]):
    class Meta:
        model = Region
        fields = (
            "id",
            "name",
            "latitude",
            "longitude",
            "timezone",
            "revision",
            "status",
        )
        read_only_fields = fields


class LocationChangeInput(InputSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    latitude = FiniteFloat(min_value=-90, max_value=90)
    longitude = FiniteFloat(min_value=-180, max_value=180)
    timezone = serializers.CharField(max_length=100)
    reason = serializers.CharField(max_length=1000, required=False, default="", allow_blank=True)


class LocationChangeOutput(serializers.ModelSerializer[RegionLocationChangeRequest]):
    class Meta:
        model = RegionLocationChangeRequest
        fields = (
            "id",
            "base_revision",
            "latitude",
            "longitude",
            "timezone",
            "reason",
            "status",
            "created_at",
            "reviewed_at",
            "decision_reason",
        )
        read_only_fields = fields


class ForecastQuery(InputSerializer):
    date = serializers.DateField()
    data_source = serializers.ChoiceField(
        choices=("auto", *SnapshotSourceKind.values, "archived_forecast"), default="auto"
    )


def _optional_number() -> serializers.FloatField:
    return serializers.FloatField(allow_null=True, required=False)


def _flags() -> serializers.ListField:
    return serializers.ListField(child=serializers.CharField(), required=False)


def _snapshot_ids() -> serializers.ListField:
    return serializers.ListField(child=serializers.IntegerField(), required=False)


class TimeWindowOutput(serializers.Serializer[dict[str, Any]]):
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()


class DaylightOutput(serializers.Serializer[dict[str, Any]]):
    policy = serializers.CharField(required=False)
    energy_scope = serializers.CharField(required=False)
    status = serializers.ChoiceField(choices=("normal", "polar_day", "polar_night"))
    sunrise = serializers.DateTimeField(allow_null=True, required=False)
    sunset = serializers.DateTimeField(allow_null=True, required=False)
    windows = TimeWindowOutput(many=True, required=False)
    duration_seconds = _optional_number()
    coverage_seconds = _optional_number()
    valid_coverage_seconds = _optional_number()
    coverage_fraction = _optional_number()
    input_coverage_seconds = _optional_number()
    input_coverage_fraction = _optional_number()
    complete = serializers.BooleanField(required=False)
    energy_mwh = _optional_number()
    partial_energy_mwh = _optional_number()
    energy_kwh_m2 = _optional_number()
    partial_energy_kwh_m2 = _optional_number()


class SourceSnapshotOutput(serializers.Serializer[dict[str, Any]]):
    snapshot_id = serializers.IntegerField()
    collected_at = serializers.DateTimeField()
    source_kind = serializers.CharField(required=False)


class ForecastProvenanceOutput(serializers.Serializer[dict[str, Any]]):
    selection_policy = serializers.CharField(required=False)
    snapshot_id = serializers.IntegerField(allow_null=True, required=False)
    source_kind = serializers.CharField(required=False)
    collected_at = serializers.DateTimeField(allow_null=True, required=False)
    published_at = serializers.DateTimeField(allow_null=True, required=False)
    selection_cutoff = serializers.DateTimeField(allow_null=True, required=False)
    source_snapshot_ids = _snapshot_ids()
    source_snapshots = SourceSnapshotOutput(many=True, required=False)


class WeatherReferenceOutput(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=("ready", "partial", "unavailable"))
    energy_kwh_m2 = _optional_number()
    partial_energy_kwh_m2 = _optional_number()
    coverage_fraction = _optional_number()
    quality_flags = _flags()


class IrradianceIntervalOutput(serializers.Serializer[dict[str, Any]]):
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    ghi_wm2 = _optional_number()
    quality_flags = _flags()
    source_snapshot_ids = _snapshot_ids()
    source_kinds = _flags()


class IrradianceSummaryOutput(serializers.Serializer[dict[str, Any]]):
    """Global horizontal irradiation in kWh/m2 for the local day."""

    energy_kwh_m2 = _optional_number()
    partial_energy_kwh_m2 = _optional_number()
    complete = serializers.BooleanField(required=False)
    quality_flags = _flags()


class IrradianceResultOutput(IrradianceSummaryOutput):
    local_date = serializers.DateField(required=False)
    timezone = serializers.CharField(required=False)
    start = serializers.DateTimeField(required=False)
    end = serializers.DateTimeField(required=False)
    energy_mj_m2 = _optional_number()
    partial_energy_mj_m2 = _optional_number()
    coverage_fraction = _optional_number()
    daylight = DaylightOutput(required=False)
    intervals = IrradianceIntervalOutput(many=True, required=False)
    provenance = ForecastProvenanceOutput(required=False)
    weather_reference = WeatherReferenceOutput(required=False)


# Stored results are frozen JSON from earlier code versions: the typed components document
# the contract while the payload passes through unchanged, keeping every provenance key.
@extend_schema_field(IrradianceResultOutput(allow_null=True))
class IrradianceResultField(serializers.JSONField):
    pass


class Freshness(models.TextChoices):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    ARCHIVED = "archived"


class StaleReason(models.TextChoices):
    COLLECTION_OVERDUE = "collection_overdue"
    CONNECTION_INACTIVE = "connection_inactive"


class ForecastEnvelope(serializers.Serializer[dict[str, Any]]):
    date = serializers.DateField()
    timezone = serializers.CharField()
    source_kind = serializers.ChoiceField(choices=("forecast", "estimated_actuals", "mixed"))
    freshness = serializers.ChoiceField(choices=Freshness.choices)
    stale_reason = serializers.ChoiceField(choices=StaleReason.choices, allow_null=True)
    unavailable_reason = serializers.ChoiceField(
        choices=(
            "configuration_unavailable",
            "forecast_unavailable",
            "estimated_actuals_unavailable",
        ),
        allow_null=True,
    )


class ForecastOutput(ForecastEnvelope):
    result = IrradianceResultField(allow_null=True)
    collected_at = serializers.DateTimeField(allow_null=True)
    issued_at = serializers.DateTimeField(allow_null=True)
    binding_id = serializers.IntegerField(allow_null=True)
