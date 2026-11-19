from dataclasses import dataclass
from datetime import datetime, timedelta

from pyracast.notifications.eligibility import binding_can_notify
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryError,
    ReminderSchedule,
    ReminderState,
)
from pyracast.notifications.operations import effective_recipients, recipient_limit
from pyracast.notifications.recipients import stored_recipients, workspace_recipients
from pyracast.notifications.scheduler import slot_recipients
from pyracast.regions.models import Region, RegionStatus

STATE_OF_PAUSE: dict[str, ReminderState] = {
    NotificationDeliveryError.NO_RECIPIENTS: ReminderState.NO_RECIPIENTS,
    NotificationDeliveryError.INVALID_RECIPIENT: ReminderState.INVALID_RECIPIENT,
    NotificationDeliveryError.RECIPIENT_LIMIT_EXCEEDED: ReminderState.OVER_RECIPIENT_LIMIT,
}


@dataclass(frozen=True)
class ReminderAddresses:
    recipients: list[str]
    workspace_recipients: list[str]


@dataclass(frozen=True)
class ReminderDelivery:
    id: int
    scheduled_for: datetime
    status: str
    last_error: str
    forecast_status: str | None
    forecast_complete: bool | None
    collected_at: datetime | None
    late: bool | None
    source_age_seconds: float | None
    stale: bool | None
    sent_at: datetime | None
    recipient_count: int
    recipients: list[str] | None


@dataclass(frozen=True)
class ReminderOverview:
    enabled: bool
    state: ReminderState
    include_workspace_members: bool
    language: str
    times: list[str]
    recipient_limit: int
    recipient_count: int
    workspace_member_count: int
    recipient_limit_exceeded: bool
    daily_reminder_limit: int | None
    addresses: ReminderAddresses | None
    deliveries: list[ReminderDelivery]


def reminder_state(schedule: ReminderSchedule, limit: int) -> ReminderState:
    """The scheduler's own predicates, in its order, applied to the stored schedule."""
    region, binding = schedule.region, schedule.region.approved_binding
    if not schedule.enabled:
        return ReminderState.PAUSED_BY_USER
    if region.status != RegionStatus.APPROVED or binding is None:
        return ReminderState.WAITING_FOR_APPROVAL
    if not binding_can_notify(binding):
        account = binding.provider_account
        if not account.enabled or account.authorized_at is None:
            return ReminderState.PROVIDER_ATTENTION
        return ReminderState.WAITING_FOR_APPROVAL
    return STATE_OF_PAUSE.get(slot_recipients(schedule, limit)[1], ReminderState.SENDING)


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


def _delivery(
    delivery: NotificationDelivery, now: datetime, *, administrator: bool
) -> ReminderDelivery:
    message = delivery.prepared_message or {}
    age = _source_age(delivery, now)
    addresses = delivery.addresses()
    return ReminderDelivery(
        id=delivery.pk,
        scheduled_for=delivery.scheduled_for,
        status=delivery.status,
        last_error=delivery.last_error,
        forecast_status=message.get("kind"),
        forecast_complete=message.get("tomorrow_complete"),
        collected_at=delivery.snapshot.collected_at if delivery.snapshot else None,
        late=delivery.sent_at - delivery.scheduled_for > timedelta(seconds=60)
        if delivery.sent_at
        else None,
        source_age_seconds=age,
        stale=age > timedelta(hours=24).total_seconds() if age is not None else None,
        sent_at=delivery.sent_at,
        recipient_count=len(addresses),
        recipients=addresses if administrator else None,
    )


def reminder_overview(
    schedule: ReminderSchedule, now: datetime, *, administrator: bool
) -> ReminderOverview:
    """What the reminder screen shows; viewers get counts, only administrators addresses."""
    limit = recipient_limit()
    effective = effective_recipients(schedule)
    members = workspace_recipients(schedule.region.tenant_id)
    binding = schedule.region.approved_binding
    deliveries = (
        NotificationDelivery.objects.filter(schedule__region_id=schedule.region_id)
        .select_related("snapshot")
        .prefetch_related("recipients")
        .order_by("-scheduled_for", "-pk")[:20]
    )
    return ReminderOverview(
        enabled=schedule.enabled,
        state=reminder_state(schedule, limit),
        include_workspace_members=schedule.include_workspace_members,
        language=schedule.language,
        times=list(schedule.times),
        recipient_limit=limit,
        recipient_count=len(effective),
        workspace_member_count=len(members),
        recipient_limit_exceeded=len(effective) > limit,
        daily_reminder_limit=binding.provider_account.daily_quota if binding else None,
        addresses=ReminderAddresses(stored_recipients(schedule), members)
        if administrator
        else None,
        deliveries=[
            _delivery(delivery, now, administrator=administrator) for delivery in deliveries
        ],
    )


def plants_at_recipient_limit(tenant_id: int) -> int:
    """Plants whose reminders include the members, are sending and already reach the limit, so
    one more member pauses them."""
    limit = recipient_limit()
    return sum(
        reminder_state(schedule, limit) == ReminderState.SENDING
        and len(effective_recipients(schedule)) == limit
        for schedule in ReminderSchedule.objects.filter(
            region__in=Region.objects.active().filter(tenant_id=tenant_id),
            enabled=True,
            include_workspace_members=True,
        ).select_related("region__approved_binding__provider_account")
    )
