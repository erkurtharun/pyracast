from rest_framework import serializers

from pyracast.notifications.operations import RECIPIENT_MAX_LENGTH


class ReminderInput(serializers.Serializer[dict[str, object]]):
    enabled = serializers.BooleanField()
    language = serializers.ChoiceField(choices=("en", "tr"))
    recipients = serializers.ListField(
        child=serializers.EmailField(max_length=RECIPIENT_MAX_LENGTH)
    )
    times = serializers.ListField(
        child=serializers.RegexField(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"), max_length=1440
    )


class ReminderQuotaOutput(serializers.Serializer[dict[str, object]]):
    daily_reminder_limit = serializers.IntegerField()
    queries_per_email = serializers.IntegerField()


class ReminderDeliveryOutput(serializers.Serializer[dict[str, object]]):
    id = serializers.IntegerField()
    recipient = serializers.EmailField(allow_blank=True)
    scheduled_for = serializers.DateTimeField()
    status = serializers.CharField()
    last_error = serializers.CharField(allow_blank=True)
    forecast_status = serializers.CharField(allow_null=True)
    forecast_complete = serializers.BooleanField(allow_null=True)
    collected_at = serializers.DateTimeField(allow_null=True)
    late = serializers.BooleanField(allow_null=True)
    source_age_seconds = serializers.FloatField(allow_null=True)
    stale = serializers.BooleanField(allow_null=True)
    sent_at = serializers.DateTimeField(allow_null=True)


class ReminderOutput(serializers.Serializer[dict[str, object]]):
    enabled = serializers.BooleanField()
    language = serializers.ChoiceField(choices=("en", "tr"))
    times = serializers.ListField(child=serializers.CharField())
    recipients = serializers.ListField(child=serializers.EmailField())
    effective_recipients = serializers.ListField(child=serializers.CharField())
    recipient_limit = serializers.IntegerField()
    recipient_count = serializers.IntegerField()
    recipient_slots_remaining = serializers.IntegerField()
    recipient_limit_exceeded = serializers.BooleanField()
    timezone = serializers.CharField()
    quota = ReminderQuotaOutput(allow_null=True)
    deliveries = ReminderDeliveryOutput(many=True)
