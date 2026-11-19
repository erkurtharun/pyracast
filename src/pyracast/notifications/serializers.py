from rest_framework import serializers

from pyracast.accounts.models import Language
from pyracast.api.serializers import InputSerializer
from pyracast.connections.models import MAX_DAILY_QUOTA
from pyracast.notifications.models import (
    NotificationDeliveryError,
    NotificationDeliveryStatus,
    PreparedMessageKind,
    ReminderState,
)
from pyracast.notifications.operations import MAX_RECIPIENTS_PER_SAVE, RECIPIENT_MAX_LENGTH
from pyracast.notifications.overview import ReminderAddresses, ReminderDelivery, ReminderOverview


class ReminderInput(InputSerializer):
    enabled = serializers.BooleanField()
    include_workspace_members = serializers.BooleanField()
    language = serializers.ChoiceField(choices=Language.choices)
    recipients = serializers.ListField(
        child=serializers.EmailField(max_length=RECIPIENT_MAX_LENGTH),
        max_length=MAX_RECIPIENTS_PER_SAVE,
    )
    times = serializers.ListField(
        child=serializers.RegexField(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"),
        max_length=MAX_DAILY_QUOTA,
    )


class ReminderDeliveryOutput(serializers.Serializer[ReminderDelivery]):
    id = serializers.IntegerField()
    scheduled_for = serializers.DateTimeField()
    status = serializers.ChoiceField(choices=NotificationDeliveryStatus.choices)
    last_error = serializers.ChoiceField(
        choices=NotificationDeliveryError.choices, allow_blank=True
    )
    forecast_status = serializers.ChoiceField(choices=PreparedMessageKind.choices, allow_null=True)
    forecast_complete = serializers.BooleanField(allow_null=True)
    collected_at = serializers.DateTimeField(allow_null=True)
    late = serializers.BooleanField(allow_null=True)
    source_age_seconds = serializers.FloatField(allow_null=True)
    stale = serializers.BooleanField(allow_null=True)
    sent_at = serializers.DateTimeField(allow_null=True)
    recipient_count = serializers.IntegerField()
    recipients = serializers.ListField(child=serializers.EmailField(), allow_null=True)


class ReminderAddressesOutput(serializers.Serializer[ReminderAddresses]):
    recipients = serializers.ListField(child=serializers.EmailField())
    workspace_recipients = serializers.ListField(child=serializers.EmailField())


class ReminderOutput(serializers.Serializer[ReminderOverview]):
    enabled = serializers.BooleanField()
    state = serializers.ChoiceField(choices=ReminderState.choices)
    include_workspace_members = serializers.BooleanField()
    language = serializers.ChoiceField(choices=Language.choices)
    times = serializers.ListField(child=serializers.CharField())
    recipient_limit = serializers.IntegerField()
    recipient_count = serializers.IntegerField()
    workspace_member_count = serializers.IntegerField()
    recipient_limit_exceeded = serializers.BooleanField()
    daily_reminder_limit = serializers.IntegerField(allow_null=True)
    addresses = ReminderAddressesOutput(allow_null=True)
    deliveries = ReminderDeliveryOutput(many=True)
