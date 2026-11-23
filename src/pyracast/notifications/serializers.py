from typing import Any

from django.utils.translation import gettext
from rest_framework import serializers

from pyracast.accounts.models import Language
from pyracast.api.serializers import InputSerializer
from pyracast.connections.models import MAX_DAILY_QUOTA
from pyracast.notifications.models import (
    NotificationDeliveryError,
    NotificationDeliveryStatus,
    PreparedMessageKind,
    RecipientRole,
    ReminderState,
)
from pyracast.notifications.operations import (
    MAX_RECIPIENT_LIMIT,
    RECIPIENT_MAX_LENGTH,
    AddressChoice,
    MemberChoice,
)
from pyracast.notifications.overview import (
    AddedAddress,
    AddressRole,
    MemberAddress,
    MemberKind,
    ReminderAddresses,
    ReminderDelivery,
    ReminderOutcome,
    ReminderOverview,
    ReminderSummary,
)
from pyracast.notifications.recipients import mailbox_key


class AddressRoleInput(InputSerializer):
    address = serializers.EmailField(max_length=RECIPIENT_MAX_LENGTH)
    role = serializers.ChoiceField(choices=RecipientRole.choices)
    memberships = serializers.ListField(
        child=serializers.IntegerField(min_value=1), max_length=MAX_RECIPIENT_LIMIT
    )


class MemberRoleInput(InputSerializer):
    memberships = serializers.ListField(
        child=serializers.IntegerField(min_value=1), max_length=MAX_RECIPIENT_LIMIT
    )
    address = serializers.EmailField(max_length=RECIPIENT_MAX_LENGTH)
    role = serializers.ChoiceField(choices=RecipientRole.choices, allow_null=True)


class ReminderInput(InputSerializer):
    enabled = serializers.BooleanField()
    include_workspace_members = serializers.BooleanField()
    language = serializers.ChoiceField(choices=Language.choices)
    recipients = serializers.ListField(
        child=serializers.EmailField(max_length=RECIPIENT_MAX_LENGTH),
        max_length=MAX_RECIPIENT_LIMIT,
    )
    times = serializers.ListField(
        child=serializers.RegexField(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"),
        max_length=MAX_DAILY_QUOTA,
    )
    expected_revision = serializers.IntegerField(min_value=0)
    hide_recipients = serializers.BooleanField(required=False)
    # A list field publishes its bound as maxItems; a list serializer's bound is not published.
    address_roles = serializers.ListField(
        child=AddressRoleInput(), required=False, max_length=MAX_RECIPIENT_LIMIT
    )
    member_roles = serializers.ListField(
        child=MemberRoleInput(), required=False, max_length=MAX_RECIPIENT_LIMIT
    )

    def validate_member_roles(self, value: list[dict[str, Any]]) -> list[MemberChoice]:
        keys = {mailbox_key(entry["address"]) for entry in value}
        memberships = [pk for entry in value for pk in set(entry["memberships"])]
        if len(set(memberships)) != len(memberships) or len(keys) != len(value):
            raise serializers.ValidationError(gettext("Choose each member's role once."))
        return [
            MemberChoice(
                frozenset(entry["memberships"]),
                entry["address"],
                None if entry["role"] is None else RecipientRole(entry["role"]),
            )
            for entry in value
        ]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if (entries := attrs.get("address_roles")) is None:
            return attrs
        keys = [mailbox_key(entry["address"]) for entry in entries]
        if len(set(keys)) != len(keys) or not set(keys) <= {
            mailbox_key(address) for address in attrs["recipients"]
        }:
            raise serializers.ValidationError(
                {"address_roles": gettext("Choose a role once for each address of the recipients.")}
            )
        attrs["address_roles"] = [
            AddressChoice(
                entry["address"], RecipientRole(entry["role"]), frozenset(entry["memberships"])
            )
            for entry in entries
        ]
        return attrs


class AddressRoleOutput(serializers.Serializer[AddressRole]):
    address = serializers.EmailField()
    role = serializers.ChoiceField(choices=RecipientRole.choices)


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
    recipient_roles = AddressRoleOutput(many=True, allow_null=True)


class MemberAddressOutput(serializers.Serializer[MemberAddress]):
    memberships = serializers.ListField(child=serializers.IntegerField())
    address = serializers.EmailField()
    kind = serializers.ChoiceField(choices=[kind.value for kind in MemberKind])
    role = serializers.ChoiceField(choices=RecipientRole.choices, allow_null=True)


class AddedAddressOutput(serializers.Serializer[AddedAddress]):
    address = serializers.EmailField()
    role = serializers.ChoiceField(choices=RecipientRole.choices, allow_null=True)
    eligible = serializers.BooleanField()
    memberships = serializers.ListField(child=serializers.IntegerField())


class ReminderAddressesOutput(serializers.Serializer[ReminderAddresses]):
    recipients = serializers.ListField(child=serializers.EmailField())
    workspace_recipients = serializers.ListField(child=serializers.EmailField())
    members = MemberAddressOutput(many=True)
    added = AddedAddressOutput(many=True)


class ReminderOutput(serializers.Serializer[ReminderOverview]):
    enabled = serializers.BooleanField()
    state = serializers.ChoiceField(choices=ReminderState.choices)
    revision = serializers.IntegerField()
    include_workspace_members = serializers.BooleanField()
    hide_recipients = serializers.BooleanField()
    language = serializers.ChoiceField(choices=Language.choices)
    times = serializers.ListField(child=serializers.CharField())
    recipient_limit = serializers.IntegerField()
    recipient_count = serializers.IntegerField()
    workspace_member_count = serializers.IntegerField()
    recipient_limit_exceeded = serializers.BooleanField()
    daily_reminder_limit = serializers.IntegerField(allow_null=True)
    addresses = ReminderAddressesOutput(allow_null=True)
    deliveries = ReminderDeliveryOutput(many=True)


class ReminderOutcomeOutput(serializers.Serializer[ReminderOutcome]):
    scheduled_for = serializers.DateTimeField()
    status = serializers.ChoiceField(choices=NotificationDeliveryStatus.choices)
    last_error = serializers.ChoiceField(
        choices=NotificationDeliveryError.choices, allow_blank=True
    )


class ReminderSummaryOutput(serializers.Serializer[ReminderSummary]):
    state = serializers.ChoiceField(choices=ReminderState.choices)
    last_delivery = ReminderOutcomeOutput(allow_null=True)
