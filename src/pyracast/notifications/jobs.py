import logging
import secrets
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.background.failures import log_failure
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
)
from pyracast.notifications.operations import recipient_limit
from pyracast.notifications.preparation import prepare_delivery
from pyracast.notifications.recipients import permitted_recipients
from pyracast.notifications.scheduler import MAX_LATENESS, slot_recipients
from pyracast.regions.models import Region, active_region

LEASE_DURATION = timedelta(minutes=5)
logger = logging.getLogger(__name__)


def _skip_reason(delivery: NotificationDelivery, limit: int) -> str | None:
    schedule = delivery.schedule
    clock = delivery.scheduled_for.astimezone(ZoneInfo(schedule.region.timezone)).strftime("%H:%M")
    if not schedule.enabled or clock not in schedule.times:
        return NotificationDeliveryError.CANCELLED_BY_SETTINGS_CHANGE
    if reason := slot_recipients(schedule, limit)[1]:
        return reason
    delivery.recipients = permitted_recipients(delivery)
    if not delivery.recipients:
        return NotificationDeliveryError.NO_RECIPIENTS
    if (
        schedule.region_id != delivery.binding.region_id
        or clock not in schedule.times[: delivery.binding.provider_account.daily_quota]
        or not binding_can_notify(delivery.binding)
    ):
        return NotificationDeliveryError.SCHEDULE_OR_AUTHORITY_CHANGED
    return None


def _recover_interrupted(delivery: NotificationDelivery, now: datetime) -> None:
    if delivery.transmit_started_at is not None:
        delivery.status, delivery.last_error = (
            NotificationDeliveryStatus.UNCERTAIN,
            NotificationDeliveryError.INTERRUPTED_SEND,
        )
    else:
        delivery.status = (
            NotificationDeliveryStatus.RETRY
            if delivery.attempts < 8 and now < delivery.scheduled_for + MAX_LATENESS
            else NotificationDeliveryStatus.FAILED
        )
        delivery.last_error = NotificationDeliveryError.INTERRUPTED_CONNECTION
        delivery.available_at = now
        delivery.lease_token = None
        delivery.lease_expires_at = None
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


def _mark_transmit(
    delivery_id: int, token: uuid.UUID, now: datetime, message: EmailMultiAlternatives
) -> bool:
    started = time.monotonic()
    with transaction.atomic():
        _lock_workspace(delivery_id)
        limit = recipient_limit(lock=True)
        delivery = _locked_delivery(delivery_id)
        if (
            delivery is None
            or delivery.status != NotificationDeliveryStatus.SENDING
            or delivery.lease_token != token
        ):
            return False
        reason = _skip_reason(delivery, limit)
        now += timedelta(seconds=time.monotonic() - started)
        if delivery.lease_expires_at is None or delivery.lease_expires_at <= now:
            _recover_interrupted(delivery, now)
            return False
        if reason is not None or now - delivery.scheduled_for > MAX_LATENESS:
            delivery.status = NotificationDeliveryStatus.SKIPPED
            delivery.last_error = reason or NotificationDeliveryError.DELIVERY_DEADLINE_EXCEEDED
            delivery.lease_token = None
            delivery.lease_expires_at = None
            delivery.save()
            return False
        delivery.transmit_started_at = now
        delivery.save(update_fields=("recipients", "transmit_started_at"))
        message.bcc = list(delivery.recipients)
        return True


def process_notification(
    delivery_id: int,
    now: datetime,
    *,
    sender: Callable[[EmailMultiAlternatives], None] | None = None,
) -> bool:
    started = time.monotonic()
    entered_at = now
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    with transaction.atomic():
        _lock_workspace(delivery_id)
        limit = recipient_limit(lock=True)
        delivery = _locked_delivery(delivery_id)
        if delivery is None:
            return False
        now = entered_at + timedelta(seconds=time.monotonic() - started)
        if delivery.status == NotificationDeliveryStatus.SENDING:
            if delivery.lease_expires_at is None or delivery.lease_expires_at <= now:
                _recover_interrupted(delivery, now)
            return False
        if (
            delivery.status
            not in {NotificationDeliveryStatus.PENDING, NotificationDeliveryStatus.RETRY}
            or delivery.available_at > now
        ):
            return False
        if (reason := _skip_reason(delivery, limit)) is not None:
            delivery.status, delivery.last_error = NotificationDeliveryStatus.SKIPPED, reason
            delivery.save()
            return False
        if now - delivery.scheduled_for > MAX_LATENESS:
            delivery.status, delivery.last_error = (
                NotificationDeliveryStatus.SKIPPED,
                NotificationDeliveryError.MISSED_SCHEDULE,
            )
            delivery.save()
            return False
        frozen = cast(dict[str, object] | None, delivery.prepared_message)
        frozen_snapshot_id = delivery.snapshot_id
        try:
            if not prepare_delivery(delivery, now):
                return False
            message = build_message(delivery)
        except DatabaseError:
            raise
        except Exception as error:
            log_failure(logger, "notification_preparation_failed", error, delivery_id=delivery.pk)
            prepared_at = now
            delivery.snapshot_id = None
            if frozen is not None:
                delivery.snapshot_id = frozen_snapshot_id
                prepared_at = datetime.fromisoformat(
                    cast(str, frozen.get("prepared_at", delivery.scheduled_for.isoformat()))
                )
            message, delivery.prepared_message = build_unavailable_message(delivery, prepared_at)
        token = uuid.uuid4()
        delivery.status, delivery.lease_token = NotificationDeliveryStatus.SENDING, token
        delivery.lease_expires_at = now + LEASE_DURATION
        delivery.transmit_started_at = None
        delivery.attempts += 1
        delivery.save()
    available_at = now
    error_code: str
    transmitting = False

    def before_send() -> bool:
        nonlocal transmitting
        transmitting = _mark_transmit(
            delivery_id,
            token,
            entered_at + timedelta(seconds=time.monotonic() - started),
            message,
        )
        return transmitting

    def retry_or_fail() -> tuple[NotificationDeliveryStatus, datetime]:
        completed_at = entered_at + timedelta(seconds=time.monotonic() - started)
        retry_at = completed_at + timedelta(
            seconds=min(60 * 2 ** (delivery.attempts - 1), 1800) + secrets.randbelow(31)
        )
        if delivery.attempts < 8 and retry_at < delivery.scheduled_for + MAX_LATENESS:
            return NotificationDeliveryStatus.RETRY, retry_at
        return NotificationDeliveryStatus.FAILED, now

    try:
        if sender is None:
            send(message, before_send=before_send)
        elif before_send():
            sender(message)
        else:
            return False
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
    updated = NotificationDelivery.objects.filter(
        pk=delivery_id,
        lease_token=token,
        status__in=(NotificationDeliveryStatus.SENDING, NotificationDeliveryStatus.UNCERTAIN),
    ).update(
        status=status,
        last_error=error_code,
        sent_at=timezone.now() if status == NotificationDeliveryStatus.SENT else None,
        available_at=available_at,
        lease_token=None,
        lease_expires_at=None,
    )
    return status == NotificationDeliveryStatus.SENT and bool(updated)


def pending_notification_ids(now: datetime, limit: int = 100) -> list[int]:
    return list(
        NotificationDelivery.objects.filter(
            active_region("schedule__region__"), available_at__lte=now
        )
        .filter(
            Q(status__in=(NotificationDeliveryStatus.PENDING, NotificationDeliveryStatus.RETRY))
            | Q(status=NotificationDeliveryStatus.SENDING, lease_expires_at__lte=now)
        )
        .order_by("available_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
