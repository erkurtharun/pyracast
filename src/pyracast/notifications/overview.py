from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pyracast.notifications.addressing import explicit_role
from pyracast.notifications.eligibility import notifiable_regions
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryError,
    RecipientRole,
    ReminderSchedule,
    ReminderState,
)
from pyracast.notifications.operations import recipient_limit
from pyracast.notifications.recipients import (
    Explicit,
    Workspace,
    candidates,
    delivery_addresses,
    delivery_roles,
    effective,
    explicit_recipients,
    recipient_key,
    stored_choices,
    workspace,
)
from pyracast.notifications.scheduler import recipient_refusal
from pyracast.regions.models import Region, RegionStatus

STATE_OF_PAUSE: dict[str, ReminderState] = {
    NotificationDeliveryError.NO_RECIPIENTS: ReminderState.NO_RECIPIENTS,
    NotificationDeliveryError.INVALID_RECIPIENT: ReminderState.INVALID_RECIPIENT,
    NotificationDeliveryError.RECIPIENT_LIMIT_EXCEEDED: ReminderState.OVER_RECIPIENT_LIMIT,
}


class MemberKind(StrEnum):
    ADMINISTRATOR = "administrator"
    MEMBER = "member"


@dataclass(frozen=True)
class MemberAddress:
    membership: int
    address: str
    kind: MemberKind
    role: RecipientRole | None


@dataclass(frozen=True)
class AddedAddress:
    """`role` is the explicit choice the rule applies to the address, `membership` the active
    member whose verified primary address it is."""

    address: str
    role: RecipientRole | None
    eligible: bool
    membership: int | None


@dataclass(frozen=True)
class AddressRole:
    address: str
    role: RecipientRole


@dataclass(frozen=True)
class ReminderAddresses:
    recipients: list[str]
    workspace_recipients: list[str]
    members: list[MemberAddress]
    added: list[AddedAddress]


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
    recipient_roles: list[AddressRole] | None


@dataclass(frozen=True)
class ReminderOverview:
    enabled: bool
    state: ReminderState
    revision: int
    include_workspace_members: bool
    hide_recipients: bool
    language: str
    times: list[str]
    recipient_limit: int
    recipient_count: int
    workspace_member_count: int
    recipient_limit_exceeded: bool
    daily_reminder_limit: int | None
    addresses: ReminderAddresses | None
    deliveries: list[ReminderDelivery]


@dataclass(frozen=True)
class ReminderOutcome:
    scheduled_for: datetime
    status: str
    last_error: str


@dataclass(frozen=True)
class ReminderSummary:
    state: ReminderState
    last_delivery: ReminderOutcome | None


@dataclass(frozen=True)
class Assessment:
    schedule: ReminderSchedule
    explicit: list[Explicit]
    recipients: list[str]
    state: ReminderState


def reminder_state(schedule: ReminderSchedule, *, notifiable: bool, refusal: str) -> ReminderState:
    """The scheduler's own predicates, in its order, applied to the stored schedule."""
    region, binding = schedule.region, schedule.region.approved_binding
    if not schedule.enabled:
        return ReminderState.PAUSED_BY_USER
    if region.status != RegionStatus.APPROVED or binding is None:
        return ReminderState.WAITING_FOR_APPROVAL
    if not notifiable:
        account = binding.provider_account
        if not account.enabled or account.authorized_at is None:
            return ReminderState.PROVIDER_ATTENTION
        return ReminderState.WAITING_FOR_APPROVAL
    return STATE_OF_PAUSE.get(refusal, ReminderState.SENDING)


def assess(
    schedules: Sequence[ReminderSchedule], members: Workspace, limit: int
) -> list[Assessment]:
    """Recipients and state of schedules of one workspace, in a fixed number of queries."""
    explicit = explicit_recipients(schedule.pk for schedule in schedules)
    notifiable = notifiable_regions(schedule.region_id for schedule in schedules)
    assessed = []
    for schedule in schedules:
        rows = explicit.get(schedule.pk, [])
        recipients = effective(schedule.include_workspace_members, rows, members)
        state = reminder_state(
            schedule,
            notifiable=schedule.region_id in notifiable,
            refusal=recipient_refusal(recipients, limit),
        )
        assessed.append(Assessment(schedule, rows, recipients, state))
    return assessed


def _source_age(delivery: NotificationDelivery, now: datetime) -> float | None:
    if delivery.snapshot is None:
        return None
    basis = delivery.prepared_at or delivery.sent_at or now
    return max(0.0, (basis - delivery.snapshot.collected_at).total_seconds())


def _delivery(
    delivery: NotificationDelivery, now: datetime, *, administrator: bool
) -> ReminderDelivery:
    age = _source_age(delivery, now)
    addresses = delivery_addresses(delivery)
    roles = delivery_roles(delivery) if administrator else None
    return ReminderDelivery(
        id=delivery.pk,
        scheduled_for=delivery.scheduled_for,
        status=delivery.status,
        last_error=delivery.last_error,
        forecast_status=delivery.forecast_kind or None,
        forecast_complete=delivery.forecast_complete,
        collected_at=delivery.snapshot.collected_at if delivery.snapshot else None,
        late=delivery.sent_at - delivery.scheduled_for > timedelta(seconds=60)
        if delivery.sent_at
        else None,
        source_age_seconds=age,
        stale=age > timedelta(hours=24).total_seconds() if age is not None else None,
        sent_at=delivery.sent_at,
        recipient_count=len(addresses),
        recipients=addresses if administrator else None,
        recipient_roles=None
        if roles is None
        else [AddressRole(address, role) for address, role in roles],
    )


def _addresses(
    schedule: ReminderSchedule, assessment: Assessment, members: Workspace
) -> ReminderAddresses:
    explicit = assessment.explicit
    choices = stored_choices(schedule, explicit)
    typed = candidates([row.address for row in explicit], explicit, members, choices)
    holding = {recipient_key(member.address): member.membership for member in members.members}
    return ReminderAddresses(
        [row.address for row in explicit],
        list(members.addresses),
        [
            MemberAddress(
                member.membership,
                member.address,
                MemberKind.ADMINISTRATOR if member.administrator else MemberKind.MEMBER,
                choices.chosen.get(member.membership),
            )
            for member in members.members
        ],
        [
            AddedAddress(
                row.address,
                explicit_role(candidate),
                not row.ties or bool(members.active & row.ties),
                holding.get(recipient_key(row.address)),
            )
            for row, candidate in zip(explicit, typed, strict=True)
        ],
    )


def reminder_overview(
    schedule: ReminderSchedule, now: datetime, *, administrator: bool
) -> ReminderOverview:
    """What the reminder screen shows; viewers get counts, only administrators addresses."""
    limit = recipient_limit()
    members = workspace(schedule.region.tenant_id)
    (assessment,) = assess([schedule], members, limit)
    binding = schedule.region.approved_binding
    deliveries = (
        NotificationDelivery.objects.filter(schedule__region_id=schedule.region_id)
        .defer("prepared_message")
        .select_related("snapshot")
        .prefetch_related("recipient_rows")
        .order_by("-scheduled_for", "-pk")[:20]
    )
    return ReminderOverview(
        enabled=schedule.enabled,
        state=assessment.state,
        revision=schedule.revision,
        include_workspace_members=schedule.include_workspace_members,
        hide_recipients=schedule.hide_recipients and schedule.pk is not None,
        language=schedule.language,
        times=list(schedule.times),
        recipient_limit=limit,
        recipient_count=len(assessment.recipients),
        workspace_member_count=len(members.addresses),
        recipient_limit_exceeded=len(assessment.recipients) > limit,
        daily_reminder_limit=binding.provider_account.daily_quota if binding else None,
        addresses=_addresses(schedule, assessment, members) if administrator else None,
        deliveries=[
            _delivery(delivery, now, administrator=administrator) for delivery in deliveries
        ],
    )


def reminder_summaries(tenant_id: int, region_ids: Sequence[int]) -> dict[int, ReminderSummary]:
    """Each saved schedule's state and latest delivery outcome, keyed by region."""
    schedules = list(
        ReminderSchedule.objects.filter(
            region_id__in=region_ids, region__tenant_id=tenant_id
        ).select_related("region__approved_binding__provider_account")
    )
    if not schedules:
        return {}
    latest = {
        delivery.schedule_id: ReminderOutcome(
            delivery.scheduled_for, delivery.status, delivery.last_error
        )
        for delivery in NotificationDelivery.objects.filter(schedule__in=schedules)
        .order_by("schedule_id", "-scheduled_for", "-pk")
        .distinct("schedule_id")
        .only("schedule", "scheduled_for", "status", "last_error")
    }
    return {
        assessment.schedule.region_id: ReminderSummary(
            assessment.state, latest.get(assessment.schedule.pk)
        )
        for assessment in assess(schedules, workspace(tenant_id), recipient_limit())
    }


def plants_at_recipient_limit(tenant_id: int) -> int:
    """Plants whose reminders include the members, are sending and already reach the limit, so
    one more member pauses them."""
    schedules = list(
        ReminderSchedule.objects.filter(
            region__in=Region.objects.active().filter(tenant_id=tenant_id),
            enabled=True,
            include_workspace_members=True,
        ).select_related("region__approved_binding__provider_account")
    )
    if not schedules:
        return 0
    limit = recipient_limit()
    return sum(
        assessment.state == ReminderState.SENDING and len(assessment.recipients) == limit
        for assessment in assess(schedules, workspace(tenant_id), limit)
    )
