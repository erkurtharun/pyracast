import logging
import uuid
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.mail import EmailMultiAlternatives
from django.db import DatabaseError, transaction
from django.db.models import Case, F, Q, Value, When
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.background.claims import claim_unless_contended
from pyracast.background.clock import Clock
from pyracast.background.deadlines import ACTORS
from pyracast.background.failures import log_failure
from pyracast.background.lease import RELEASED, carries, claimable, expired, release, take
from pyracast.background.retry_policy import MAIL
from pyracast.mail.errors import (
    DeliveryUncertainError,
    MailFailure,
    NotSentError,
    PermanentlyNotSentError,
    SendCancelledError,
)
from pyracast.mail.send import send
from pyracast.notifications.eligibility import binding_can_notify
from pyracast.notifications.mail import build_message, build_unavailable_message
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryError,
    NotificationDeliveryStatus,
    PreparedMessageKind,
)
from pyracast.notifications.operations import recipient_limit
from pyracast.notifications.preparation import prepare_delivery
from pyracast.notifications.recipients import (
    Resolved,
    carries_exactly,
    headers,
    narrow_recipients,
    placements,
    record_roles,
    resolve,
)
from pyracast.notifications.scheduler import MAX_LATENESS, recipient_refusal
from pyracast.regions.models import Region, active_region

LEASE_DURATION = ACTORS["send_forecast_reminder"].lease
WAITING = (NotificationDeliveryStatus.PENDING, NotificationDeliveryStatus.RETRY)
logger = logging.getLogger(__name__)


def list_refusal(resolved: Resolved, limit: int) -> str:
    """Why the resolved list is not sent, or `""`; a read of people holding an address that is
    not plain refuses it first."""
    if resolved.members.holds_non_plain:
        return NotificationDeliveryError.INVALID_RECIPIENT
    return recipient_refusal(resolved.addresses, limit)


def _authorize(delivery: NotificationDelivery, limit: int) -> tuple[Resolved | None, str]:
    """What permits the delivery, its addresses narrowed in the database, or the skip reason."""
    schedule = delivery.schedule
    clock = delivery.scheduled_for.astimezone(ZoneInfo(schedule.region.timezone)).strftime("%H:%M")
    if not schedule.enabled or clock not in schedule.times:
        return None, NotificationDeliveryError.CANCELLED_BY_SETTINGS_CHANGE
    resolved = resolve(schedule)
    if reason := list_refusal(resolved, limit):
        return None, reason
    addresses = narrow_recipients(delivery, resolved.addresses)
    if not addresses:
        return None, NotificationDeliveryError.NO_RECIPIENTS
    if (
        schedule.region_id != delivery.binding.region_id
        or clock not in schedule.times[: delivery.binding.provider_account.daily_quota]
        or not binding_can_notify(delivery.binding)
    ):
        return None, NotificationDeliveryError.SCHEDULE_OR_AUTHORITY_CHANGED
    return replace(resolved, addresses=addresses), ""


def _recover_interrupted(delivery: NotificationDelivery, now: datetime) -> None:
    if delivery.transmit_started_at is not None:
        delivery.status, delivery.last_error = (
            NotificationDeliveryStatus.UNCERTAIN,
            NotificationDeliveryError.INTERRUPTED_SEND,
        )
    else:
        delivery.status = (
            NotificationDeliveryStatus.RETRY
            if not MAIL.exhausted(delivery.attempts) and now < delivery.scheduled_for + MAX_LATENESS
            else NotificationDeliveryStatus.FAILED
        )
        delivery.last_error = NotificationDeliveryError.INTERRUPTED_CONNECTION
        delivery.available_at = now
        release(delivery)
    delivery.save()


def _locked_delivery(delivery_id: int) -> NotificationDelivery | None:
    region_id = (
        NotificationDelivery.objects.filter(pk=delivery_id)
        .values_list("schedule__region_id", flat=True)
        .first()
    )
    if region_id is None:
        return None
    if Region.objects.select_for_update().filter(pk=region_id).first() is None:
        return None
    return NotificationDelivery.objects.select_for_update().filter(pk=delivery_id).first()


def _lock_workspace(delivery_id: int) -> None:
    tenant_id = (
        NotificationDelivery.objects.filter(pk=delivery_id)
        .values_list("schedule__region__tenant_id", flat=True)
        .first()
    )
    if tenant_id is not None:
        Tenant.objects.select_for_update().filter(pk=tenant_id).first()


def _skip(delivery: NotificationDelivery, reason: str) -> bool:
    delivery.status, delivery.last_error = NotificationDeliveryStatus.SKIPPED, reason
    release(delivery)
    delivery.save()
    return False


def _mark_transmit(
    delivery_id: int, token: uuid.UUID, now: datetime, message: EmailMultiAlternatives
) -> bool:
    clock = Clock(now)
    with transaction.atomic():
        _lock_workspace(delivery_id)
        limit = recipient_limit(lock=True)
        delivery = _locked_delivery(delivery_id)
        if delivery is None or not carries(delivery, token, NotificationDeliveryStatus.SENDING):
            return False
        resolved, reason = _authorize(delivery, limit)
        now = clock.now()
        if expired(delivery, now):
            _recover_interrupted(delivery, now)
            return False
        if resolved is None or now - delivery.scheduled_for > MAX_LATENESS:
            return _skip(delivery, reason or NotificationDeliveryError.DELIVERY_DEADLINE_EXCEEDED)
        addresses = resolved.addresses
        placed = placements(delivery.schedule, resolved.members, addresses)
        message.to, message.cc, message.bcc = sent = headers(addresses, placed)
        if not carries_exactly(message, sent):
            return _skip(delivery, NotificationDeliveryError.INVALID_RECIPIENT)
        delivery.transmit_started_at = now
        delivery.save(update_fields=("transmit_started_at",))
        record_roles(delivery, addresses, placed)
        return True


def _claim(
    delivery_id: int, clock: Clock
) -> tuple[NotificationDelivery, EmailMultiAlternatives, uuid.UUID, datetime] | None:
    with transaction.atomic():
        _lock_workspace(delivery_id)
        limit = recipient_limit(lock=True)
        delivery = _locked_delivery(delivery_id)
        if delivery is None:
            return None
        now = clock.now()
        if delivery.status == NotificationDeliveryStatus.SENDING:
            if expired(delivery, now):
                _recover_interrupted(delivery, now)
            return None
        if (
            delivery.status
            not in {NotificationDeliveryStatus.PENDING, NotificationDeliveryStatus.RETRY}
            or delivery.available_at > now
        ):
            return None
        resolved, reason = _authorize(delivery, limit)
        if resolved is None:
            delivery.status, delivery.last_error = NotificationDeliveryStatus.SKIPPED, reason
            delivery.save()
            return None
        if now - delivery.scheduled_for > MAX_LATENESS:
            delivery.status, delivery.last_error = (
                NotificationDeliveryStatus.SKIPPED,
                NotificationDeliveryError.MISSED_SCHEDULE,
            )
            delivery.save()
            return None
        frozen = delivery.prepared_message is not None
        frozen_snapshot_id, frozen_at = delivery.snapshot_id, delivery.prepared_at
        try:
            if not prepare_delivery(delivery, now):
                return None
            message = build_message(delivery)
        except DatabaseError:
            raise
        except Exception as error:
            log_failure(logger, "notification_preparation_failed", error, delivery_id=delivery.pk)
            delivery.snapshot_id, delivery.prepared_at = None, now
            if frozen:
                delivery.snapshot_id = frozen_snapshot_id
                delivery.prepared_at = frozen_at or delivery.scheduled_for
            delivery.forecast_kind = PreparedMessageKind.UNAVAILABLE
            delivery.forecast_complete = None
            message, delivery.prepared_message = build_unavailable_message(delivery)
        message.bcc = resolved.addresses
        token = take(delivery, NotificationDeliveryStatus.SENDING, now, LEASE_DURATION)
        delivery.transmit_started_at = None
        delivery.save()
        return delivery, message, token, now


def process_notification(delivery_id: int, now: datetime) -> bool:
    clock = Clock(now)
    claimed = claim_unless_contended(
        "send_forecast_reminder", delivery_id, lambda: _claim(delivery_id, clock)
    )
    if claimed is None:
        return False
    delivery, message, token, now = claimed
    available_at = now
    error_code: str
    transmitting = False

    def before_send() -> bool:
        nonlocal transmitting
        transmitting = _mark_transmit(delivery_id, token, clock.now(), message)
        return transmitting

    def retry_or_fail() -> tuple[NotificationDeliveryStatus, datetime]:
        retry_at = clock.now() + MAIL.delay(delivery.attempts)
        if (
            not MAIL.exhausted(delivery.attempts)
            and retry_at < delivery.scheduled_for + MAX_LATENESS
        ):
            return NotificationDeliveryStatus.RETRY, retry_at
        return NotificationDeliveryStatus.FAILED, now

    try:
        send(message, before_send=before_send)
    except SendCancelledError:
        return False
    except PermanentlyNotSentError as error:
        status, error_code = NotificationDeliveryStatus.FAILED, error.code
    except NotSentError as error:
        (status, available_at), error_code = retry_or_fail(), error.code
    except DeliveryUncertainError:
        status, error_code = NotificationDeliveryStatus.UNCERTAIN, MailFailure.OUTCOME_UNCERTAIN
        logger.warning("notification_delivery_uncertain delivery_id=%s", delivery_id)
    except Exception as error:
        log_failure(
            logger,
            "notification_send_failed",
            error,
            delivery_id=delivery_id,
            transmitting=transmitting,
        )
        if transmitting:
            status, error_code = NotificationDeliveryStatus.UNCERTAIN, MailFailure.OUTCOME_UNCERTAIN
        else:
            (status, available_at), error_code = retry_or_fail(), MailFailure.NOT_SENT
    else:
        status, error_code = NotificationDeliveryStatus.SENT, ""
    trashed = Q(last_error=NotificationDeliveryError.DEFINITION_TRASHED)
    stored_status: Case | NotificationDeliveryStatus = status
    stored_error: Case | str = error_code
    if status in (NotificationDeliveryStatus.RETRY, NotificationDeliveryStatus.FAILED):
        # Decided on the row version this update locks, so a trash committed meanwhile wins.
        stored_status = Case(
            When(trashed, then=Value(NotificationDeliveryStatus.CANCELLED)), default=Value(status)
        )
        stored_error = Case(When(trashed, then=F("last_error")), default=Value(error_code))
    updated = NotificationDelivery.objects.filter(
        pk=delivery_id,
        lease_token=token,
        status__in=(NotificationDeliveryStatus.SENDING, NotificationDeliveryStatus.UNCERTAIN),
    ).update(
        status=stored_status,
        last_error=stored_error,
        sent_at=timezone.now() if status == NotificationDeliveryStatus.SENT else None,
        available_at=available_at,
        **RELEASED,
    )
    return status == NotificationDeliveryStatus.SENT and bool(updated)


def pending_notification_ids(now: datetime, limit: int = 100) -> list[int]:
    return list(
        NotificationDelivery.objects.filter(
            active_region("schedule__region__"), available_at__lte=now
        )
        .filter(Q(status__in=WAITING) | claimable(NotificationDeliveryStatus.SENDING, now))
        .order_by("available_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
