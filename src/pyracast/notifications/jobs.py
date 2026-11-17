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
from pyracast.notifications.eligibility import binding_can_notify
from pyracast.notifications.mail import (
    NotSentError,
    PermanentlyNotSentError,
    SendCancelledError,
    build_message,
    build_unavailable_message,
    send_message,
)
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.operations import recipient_limit
from pyracast.notifications.preparation import prepare_delivery
from pyracast.notifications.recipients import permitted_recipients
from pyracast.notifications.scheduler import MAX_LATENESS, slot_recipients
from pyracast.regions.models import Region

LEASE_DURATION = timedelta(minutes=5)
logger = logging.getLogger(__name__)


def _skip_reason(delivery: NotificationDelivery, limit: int) -> str | None:
    schedule = delivery.schedule
    clock = delivery.scheduled_for.astimezone(ZoneInfo(schedule.region.timezone)).strftime("%H:%M")
    if not schedule.enabled or schedule.retired_at is not None or clock not in schedule.times:
        return "cancelled_by_settings_change"
    if reason := slot_recipients(schedule, limit)[1]:
        return reason
    delivery.recipients = permitted_recipients(delivery)
    if not delivery.recipients:
        return "no_recipients"
    if (
        delivery.binding is None
        or schedule.region_id != delivery.binding.region_id
        or clock not in schedule.times[: delivery.binding.provider_account.daily_quota]
        or not binding_can_notify(delivery.binding)
    ):
        return "schedule_or_authority_changed"
    return None


def _recover_interrupted(delivery: NotificationDelivery, now: datetime) -> None:
    # Legacy sends have no prepared content and cannot prove that transmission was unstarted.
    if delivery.transmit_started_at is not None or delivery.prepared_message is None:
        delivery.status, delivery.last_error = "uncertain", "interrupted_send"
    else:
        delivery.status = (
            "retry"
            if delivery.attempts < 8 and now < delivery.scheduled_for + MAX_LATENESS
            else "failed"
        )
        delivery.last_error = "interrupted_connection"
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
        if delivery is None or delivery.status != "sending" or delivery.lease_token != token:
            return False
        reason = _skip_reason(delivery, limit)
        now += timedelta(seconds=time.monotonic() - started)
        if delivery.lease_expires_at is None or delivery.lease_expires_at <= now:
            _recover_interrupted(delivery, now)
            return False
        if reason is not None or now - delivery.scheduled_for > MAX_LATENESS:
            delivery.status = "skipped"
            delivery.last_error = reason or "delivery_deadline_exceeded"
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
        if delivery.status == "sending":
            if delivery.lease_expires_at is None or delivery.lease_expires_at <= now:
                _recover_interrupted(delivery, now)
            return False
        if delivery.status not in {"pending", "retry"} or delivery.available_at > now:
            return False
        if (reason := _skip_reason(delivery, limit)) is not None:
            delivery.status, delivery.last_error = "skipped", reason
            delivery.save()
            return False
        if now - delivery.scheduled_for > MAX_LATENESS:
            delivery.status, delivery.last_error = "skipped", "missed_schedule"
            delivery.save()
            return False
        slot_deliveries = NotificationDelivery.objects.filter(
            schedule_id=delivery.schedule_id,
            binding_id=delivery.binding_id,
            scheduled_for=delivery.scheduled_for,
        )
        frozen = list(
            slot_deliveries.filter(prepared_message__isnull=False)
            .order_by("language", "pk")
            .distinct("language")
        )
        try:
            if not prepare_delivery(delivery, now):
                return False
            message = build_message(delivery)
        except DatabaseError:
            raise
        except Exception as error:
            logger.warning(
                "notification_preparation_failed delivery_id=%s error_type=%s",
                delivery.pk,
                type(error).__name__,
            )
            prepared_at = now
            delivery.snapshot = None
            if frozen:
                delivery.snapshot = frozen[0].snapshot
                existing = cast(dict[str, object], frozen[0].prepared_message)
                prepared_at = datetime.fromisoformat(
                    cast(str, existing.get("prepared_at", delivery.scheduled_for.isoformat()))
                )
            message, delivery.prepared_message = build_unavailable_message(delivery, prepared_at)
            unprepared = slot_deliveries.filter(
                attempts=0,
                status__in=("pending", "retry"),
                prepared_message__isnull=True,
            )
            payloads = {
                record.language: cast(dict[str, object], record.prepared_message)
                for record in frozen
            }
            payloads.setdefault(
                delivery.language, cast(dict[str, object], delivery.prepared_message)
            )
            for record in unprepared:
                if record.language not in payloads:
                    payloads[record.language] = dict(
                        build_unavailable_message(record, prepared_at)[1]
                    )
            for language, payload in payloads.items():
                unprepared.filter(language=language).update(
                    snapshot=delivery.snapshot, prepared_message=payload
                )
        token = uuid.uuid4()
        delivery.status, delivery.lease_token = "sending", token
        delivery.lease_expires_at = now + LEASE_DURATION
        delivery.transmit_started_at = None
        delivery.attempts += 1
        delivery.save()
    available_at = now
    try:

        def before_send() -> bool:
            return _mark_transmit(
                delivery_id,
                token,
                entered_at + timedelta(seconds=time.monotonic() - started),
                message,
            )

        if sender is None:
            send_message(message, before_send=before_send)
        elif before_send():
            sender(message)
        else:
            return False
    except SendCancelledError:
        return False
    except PermanentlyNotSentError:
        status, error_code = "failed", "mail_rejected"
    except NotSentError as error:
        completed_at = entered_at + timedelta(seconds=time.monotonic() - started)
        retry_at = completed_at + timedelta(
            seconds=min(60 * 2 ** (delivery.attempts - 1), 1800) + secrets.randbelow(31)
        )
        status = (
            "retry"
            if delivery.attempts < 8 and retry_at < delivery.scheduled_for + MAX_LATENESS
            else "failed"
        )
        available_at = retry_at if status == "retry" else now
        error_code = error.code
    except (
        Exception
    ) as error:  # SMTP acceptance may have succeeded before an exception reached the caller.
        logger.warning(
            "notification_delivery_uncertain delivery_id=%s error_type=%s",
            delivery_id,
            type(error).__name__,
        )
        status, error_code = "uncertain", "mail_outcome_uncertain"
    else:
        status, error_code = "sent", ""
    updated = NotificationDelivery.objects.filter(
        pk=delivery_id, lease_token=token, status__in=("sending", "uncertain")
    ).update(
        status=status,
        last_error=error_code,
        sent_at=timezone.now() if status == "sent" else None,
        available_at=available_at,
        lease_token=None,
        lease_expires_at=None,
    )
    return status == "sent" and bool(updated)


def pending_notification_ids(now: datetime, limit: int = 100) -> list[int]:
    return list(
        NotificationDelivery.objects.filter(
            available_at__lte=now, schedule__region__deleted_at__isnull=True
        )
        .filter(
            Q(status__in=("pending", "retry"))
            | Q(status="sending", lease_expires_at__lte=now)
            | Q(status="sending", lease_expires_at__isnull=True)
        )
        .order_by("available_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
