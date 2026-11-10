import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q

from pyracast.accounts.models import Membership
from pyracast.forecasts.jobs import binding_is_current
from pyracast.notifications.mail import NotSentError, build_message, send_message
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.scheduler import MAX_LATENESS

LEASE_DURATION = timedelta(minutes=5)


def _eligible(delivery: NotificationDelivery) -> bool:
    preference = delivery.preference
    return (
        delivery.snapshot is not None
        and delivery.snapshot.source_kind == "forecast"
        and delivery.snapshot.collected_at <= delivery.scheduled_for
        and delivery.snapshot.forecastresult.published_at <= delivery.scheduled_for
        and preference.enabled
        and preference.user.is_active
        and delivery.recipient == (preference.email or preference.user.email)
        and preference.effective_from <= delivery.scheduled_for
        and preference.region_id == delivery.snapshot.job.binding.region_id
        and delivery.scheduled_for.astimezone(ZoneInfo(preference.region.timezone)).strftime(
            "%H:%M"
        )
        in preference.times[: delivery.snapshot.job.binding.provider_account.daily_quota]
        and Membership.objects.filter(
            user=preference.user, tenant_id=preference.region.tenant_id
        ).exists()
        and binding_is_current(delivery.snapshot.job.binding)
    )


def process_notification(
    delivery_id: int,
    now: datetime,
    *,
    sender: Callable[[EmailMultiAlternatives], None] | None = None,
) -> bool:
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    with transaction.atomic():
        delivery = NotificationDelivery.objects.select_for_update().filter(pk=delivery_id).first()
        if delivery is None:
            return False
        if delivery.status == "sending":
            if delivery.lease_expires_at is None or delivery.lease_expires_at <= now:
                delivery.status, delivery.last_error = "uncertain", "interrupted_send"
                delivery.save()
            return False
        if delivery.status not in {"pending", "retry"} or delivery.available_at > now:
            return False
        if not _eligible(delivery):
            delivery.status, delivery.last_error = "skipped", "preference_or_authority_changed"
            delivery.save()
            return False
        if now - delivery.scheduled_for > MAX_LATENESS:
            delivery.status, delivery.last_error = "skipped", "missed_schedule"
            delivery.save()
            return False
        recipient = delivery.recipient
        try:
            validate_email(recipient)
        except ValidationError:
            delivery.status, delivery.last_error = "skipped", "missing_recipient"
            delivery.save()
            return False
        delivery.recipient = recipient
        try:
            message = build_message(delivery)
        except ValueError, ArithmeticError:
            delivery.status, delivery.last_error = "unavailable", "calculation_failure"
            delivery.save()
            return False
        token = uuid.uuid4()
        delivery.status, delivery.lease_token = "sending", token
        delivery.lease_expires_at = now + LEASE_DURATION
        delivery.attempts += 1
        delivery.save()
    try:
        (sender or send_message)(message)
    except NotSentError:
        status = "retry" if delivery.attempts < 3 else "failed"
        error = "mail_not_sent"
    except Exception:  # SMTP acceptance may have succeeded before an exception reached the caller.
        status, error = "uncertain", "mail_outcome_uncertain"
    else:
        status, error = "sent", ""
    NotificationDelivery.objects.filter(pk=delivery_id, lease_token=token, status="sending").update(
        status=status,
        last_error=error,
        sent_at=now if status == "sent" else None,
        available_at=now + timedelta(seconds=60),
        lease_token=None,
        lease_expires_at=None,
    )
    return status == "sent"


def pending_notification_ids(now: datetime, limit: int = 100) -> list[int]:
    return list(
        NotificationDelivery.objects.filter(
            available_at__lte=now, preference__region__deleted_at__isnull=True
        )
        .filter(
            Q(status__in=("pending", "retry"))
            | Q(status="sending", lease_expires_at__lte=now)
            | Q(status="sending", lease_expires_at__isnull=True)
        )
        .order_by("available_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
