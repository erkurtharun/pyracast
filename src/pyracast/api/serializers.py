from typing import Any, cast

from django.utils.translation import gettext
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
        choices=("auto", *SnapshotSourceKind.values), default=SnapshotSourceKind.FORECAST
    )


class ForecastEnvelope(serializers.Serializer[dict[str, Any]]):
    date = serializers.DateField()
    timezone = serializers.CharField()
    source_kind = serializers.ChoiceField(choices=("forecast", "estimated_actuals", "mixed"))
    freshness = serializers.ChoiceField(choices=("fresh", "stale", "missing"))
    unavailable_reason = serializers.ChoiceField(
        choices=(
            "configuration_unavailable",
            "forecast_unavailable",
            "estimated_actuals_unavailable",
        ),
        allow_null=True,
    )
    result = serializers.JSONField(allow_null=True)


class ForecastOutput(ForecastEnvelope):
    collected_at = serializers.DateTimeField(allow_null=True)
    issued_at = serializers.DateTimeField(allow_null=True)
    binding_id = serializers.IntegerField(allow_null=True)
