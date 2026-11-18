from typing import Any

from rest_framework import serializers

from pyracast.api.serializers import InputSerializer
from pyracast.change_requests.models import (
    ChangeRequestKind,
    ChangeRequestStatus,
    IncidentChangeAction,
    ProductionChangeAction,
)
from pyracast.incidents.api import IncidentInput
from pyracast.plants.serializers import (
    AwareDateTime,
    ProductionChangeRow,
    ProductionChangesInput,
    ProductionValueOutput,
)


class ProductionRequestInput(ProductionChangesInput):
    changes: Any = serializers.ListSerializer(
        child=ProductionChangeRow(), allow_empty=False, max_length=100
    )
    idempotency_key = serializers.UUIDField()
    reason = serializers.CharField(max_length=500)


class IncidentRequestInput(InputSerializer):
    action = serializers.ChoiceField(choices=IncidentChangeAction.choices)
    idempotency_key = serializers.UUIDField()
    reason = serializers.CharField(max_length=500, allow_blank=True, default="")
    incident_id = serializers.IntegerField(min_value=1, required=False)
    expected_revision = serializers.IntegerField(min_value=1, required=False)
    values = IncidentInput(required=False)
    ends_at = AwareDateTime(required=False)


class InboxQuery(InputSerializer):
    scope = serializers.ChoiceField(choices=("pending", "history", "mine"), default="pending")
    plant_id = serializers.IntegerField(min_value=1, required=False)


class ApprovalInput(InputSerializer):
    review_fingerprint = serializers.CharField(max_length=100)


class RejectionInput(InputSerializer):
    reason = serializers.CharField(max_length=500)


class UserOutput(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    name = serializers.CharField()


class ConflictOutput(serializers.Serializer[dict[str, Any]]):
    code = serializers.CharField()
    message = serializers.CharField()


class ProviderIntervalOutput(serializers.Serializer[dict[str, Any]]):
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    energy_mwh = serializers.FloatField()
    quality = serializers.CharField()
    source_kind = serializers.CharField()


class ProductionWarningOutput(serializers.Serializer[dict[str, Any]]):
    code = serializers.CharField()
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()


class ProductionItemOutput(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    action = serializers.ChoiceField(choices=ProductionChangeAction.choices)
    expected_revision_id = serializers.IntegerField(allow_null=True)
    baseline_energy_mwh = serializers.FloatField(allow_null=True)
    baseline_source_kind = serializers.CharField(allow_blank=True)
    current = ProductionValueOutput(allow_null=True)
    automatic = ProductionValueOutput(allow_null=True)
    provider_intervals = ProviderIntervalOutput(many=True)
    warnings = ProductionWarningOutput(many=True)
    proposed = ProductionValueOutput(allow_null=True)
    energy_mwh = serializers.FloatField(allow_null=True)
    quality = serializers.CharField()
    source = serializers.CharField()  # type: ignore[assignment]
    applied_observation_id = serializers.IntegerField(allow_null=True)


class IncidentBeforeOutput(IncidentInput):
    id = serializers.IntegerField()
    revision = serializers.IntegerField()


class IncidentChangeOutput(serializers.Serializer[dict[str, Any]]):
    action = serializers.ChoiceField(choices=IncidentChangeAction.choices)
    incident_id = serializers.IntegerField(allow_null=True)
    expected_revision = serializers.IntegerField(allow_null=True)
    values = IncidentInput(allow_null=True)
    ends_at = serializers.DateTimeField(allow_null=True)
    before = IncidentBeforeOutput(allow_null=True)
    applied_revision_id = serializers.IntegerField(allow_null=True)


class ChangeRequestSummaryOutput(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    kind = serializers.ChoiceField(choices=ChangeRequestKind.choices)
    status = serializers.ChoiceField(choices=ChangeRequestStatus.choices)
    plant_id = serializers.IntegerField()
    region_id = serializers.IntegerField()
    plant_name = serializers.CharField()
    configuration_id = serializers.IntegerField()
    proposed_by = UserOutput()
    reason = serializers.CharField(allow_blank=True)
    submitted_at = serializers.DateTimeField()
    reviewed_by = UserOutput(allow_null=True)
    closed_at = serializers.DateTimeField(allow_null=True)
    decision_reason = serializers.CharField(allow_blank=True)
    former_member = serializers.BooleanField()
    can_review = serializers.BooleanField()
    can_withdraw = serializers.BooleanField()
    unread = serializers.BooleanField()


class ChangeRequestOutput(ChangeRequestSummaryOutput):
    timezone = serializers.CharField()
    conflict = ConflictOutput(allow_null=True)
    review_fingerprint = serializers.CharField(allow_null=True)
    production_items = ProductionItemOutput(many=True)
    incident = IncidentChangeOutput(allow_null=True)
