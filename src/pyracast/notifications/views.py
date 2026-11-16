from datetime import datetime, timedelta
from typing import cast

from django.contrib.auth.models import User
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.notifications.operations import (
    configure_reminders,
    effective_recipients,
    get_reminder_schedule,
    recipient_limit,
)
from pyracast.notifications.recipients import workspace_recipients
from pyracast.notifications.serializers import ReminderInput, ReminderOutput


def _source_age(delivery: NotificationDelivery, now: datetime) -> float | None:
    if delivery.snapshot is None:
        return None
    basis = delivery.sent_at or now
    if delivery.prepared_message:
        prepared_at = delivery.prepared_message.get("prepared_at")
        if isinstance(prepared_at, str):
            try:
                prepared = datetime.fromisoformat(prepared_at)
            except ValueError:
                pass
            else:
                if prepared.utcoffset() is not None:
                    basis = prepared
    return max(0.0, (basis - delivery.snapshot.collected_at).total_seconds())


def reminder_schedule_payload(schedule: ReminderSchedule, now: datetime) -> dict[str, object]:
    recipients = effective_recipients(schedule)
    limit = recipient_limit()
    count = len(recipients)
    binding = schedule.region.approved_binding
    quota: dict[str, object] | None = None
    if binding is not None:
        account = binding.provider_account
        quota = {
            "daily_reminder_limit": account.daily_quota,
            "queries_per_email": 0,
        }
    deliveries = [
        {
            "id": delivery.pk,
            "recipients": delivery.recipients,
            "scheduled_for": delivery.scheduled_for,
            "status": delivery.status,
            "last_error": delivery.last_error,
            "forecast_status": delivery.prepared_message.get("kind")
            if delivery.prepared_message
            else None,
            "forecast_complete": delivery.prepared_message.get("tomorrow_complete")
            if delivery.prepared_message
            else None,
            "collected_at": delivery.snapshot.collected_at if delivery.snapshot else None,
            "late": delivery.sent_at - delivery.scheduled_for > timedelta(seconds=60)
            if delivery.sent_at
            else None,
            "source_age_seconds": (age := _source_age(delivery, now)),
            "stale": age > timedelta(hours=24).total_seconds() if age is not None else None,
            "sent_at": delivery.sent_at,
        }
        for delivery in NotificationDelivery.objects.filter(schedule__region_id=schedule.region_id)
        .select_related("snapshot")
        .order_by("-scheduled_for", "-pk")[:20]
    ]
    return {
        "enabled": schedule.enabled,
        "language": schedule.language,
        "times": schedule.times,
        "recipients": schedule.recipients,
        "include_workspace_members": schedule.include_workspace_members,
        "workspace_recipients": workspace_recipients(schedule.region.tenant_id),
        "effective_recipients": recipients,
        "recipient_limit": limit,
        "recipient_count": count,
        "recipient_slots_remaining": max(0, limit - count),
        "recipient_limit_exceeded": count > limit,
        "timezone": schedule.region.timezone,
        "quota": quota,
        "deliveries": deliveries,
    }


class RemindersView(APIView):
    @extend_schema(responses=ReminderOutput)
    def get(self, request: Request, tenant_id: int, region_id: int) -> Response:
        now = timezone.now()
        schedule = get_reminder_schedule(cast(User, request.user), tenant_id, region_id, now)
        return Response(reminder_schedule_payload(schedule, now))

    @extend_schema(request=ReminderInput, responses=ReminderOutput)
    def put(self, request: Request, tenant_id: int, region_id: int) -> Response:
        data = ReminderInput(data=request.data)
        data.is_valid(raise_exception=True)
        now = timezone.now()
        schedule = configure_reminders(
            cast(User, request.user), tenant_id, region_id, now=now, **data.validated_data
        )
        return Response(reminder_schedule_payload(schedule, now))
