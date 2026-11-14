from typing import Any, cast

from django.utils.translation import gettext
from rest_framework import serializers

from pyracast.accounts.models import Membership, Tenant
from pyracast.regions.models import Region, RegionLocationChangeRequest


class InputSerializer(serializers.Serializer[dict[str, Any]]):
    def to_internal_value(self, data: Any) -> dict[str, Any]:
        if isinstance(data, dict) and data.keys() - self.fields.keys():
            raise serializers.ValidationError(
                {"non_field_errors": [gettext("Unknown fields are not accepted.")]}
            )
        return cast(dict[str, Any], super().to_internal_value(data))


class TenantOutput(serializers.ModelSerializer[Tenant]):
    role = serializers.ChoiceField(choices=Membership.Role.choices, read_only=True)

    class Meta:
        model = Tenant
        fields = ("id", "name", "role")
        read_only_fields = fields


class RegionInput(InputSerializer):
    name = serializers.CharField(max_length=200)
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
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


class RegionRenameInput(InputSerializer):
    name = serializers.CharField(max_length=200)


class LocationChangeInput(InputSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
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
        )
        read_only_fields = fields


class AccountInput(InputSerializer):
    provider = serializers.ChoiceField(choices=("fixture", "solcast"))
    external_account_id = serializers.CharField(max_length=200)
    api_key = serializers.CharField(
        max_length=1024, write_only=True, trim_whitespace=False, allow_blank=True, default=""
    )
    daily_quota = serializers.IntegerField(min_value=1, max_value=10, default=10)
    authorization_reference = serializers.CharField(max_length=500, required=False, default="")


class RotateSecretInput(InputSerializer):
    api_key = serializers.CharField(max_length=1024, write_only=True, trim_whitespace=False)


class BindingInput(InputSerializer):
    provider_account_id = serializers.IntegerField(min_value=1)
    site_id = serializers.CharField(max_length=200)
    capacity_kw = serializers.FloatField(min_value=0)
    dc_capacity_kw = serializers.FloatField(min_value=0, allow_null=True, default=None)
    power_basis = serializers.ChoiceField(choices=("ac", "dc"), default="ac")
    capacity_semantics_verified = serializers.BooleanField()
    tilt_degrees = serializers.FloatField(default=0)
    azimuth_degrees = serializers.FloatField(default=0)
    losses_percent = serializers.FloatField(default=0)
    installation_date = serializers.DateField(required=True)
    settings_verification_reference = serializers.CharField(
        max_length=500, required=False, default=""
    )


class ApprovalInput(InputSerializer):
    binding_id = serializers.IntegerField(min_value=1)


class TransitionInput(InputSerializer):
    status = serializers.ChoiceField(choices=("rejected", "suspended"))


class IdentifierOutput(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()


class ForecastQuery(InputSerializer):
    date = serializers.DateField()
    data_source = serializers.ChoiceField(
        choices=("auto", "forecast", "estimated_actuals"), default="forecast"
    )


class ForecastOutput(serializers.Serializer[dict[str, Any]]):
    region_id = serializers.IntegerField()
    date = serializers.DateField()
    timezone = serializers.CharField()
    status = serializers.CharField()
    freshness = serializers.CharField()
    source_kind = serializers.ChoiceField(choices=("forecast", "estimated_actuals"))
    unavailable_reason = serializers.CharField(allow_null=True)
    result = serializers.JSONField(allow_null=True)
    collected_at = serializers.DateTimeField(allow_null=True)
    issued_at = serializers.DateTimeField(allow_null=True)
    binding_revision = serializers.IntegerField(allow_null=True)
    scheduled_slot = serializers.DateTimeField(allow_null=True)
    late = serializers.BooleanField(allow_null=True)
