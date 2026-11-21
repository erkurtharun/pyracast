import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from django.core.mail import EmailMultiAlternatives
from django.db import connection, transaction
from django.db.models import Q
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.account_mail import compose_account_mail
from pyracast.accounts.invitations import compose_invitation
from pyracast.accounts.mail_intents import CODE_CONTEXT
from pyracast.accounts.models import (
    ANONYMOUS_MAIL,
    EVENT_MAIL,
    OPEN_MAIL,
    InvitationDeliveryStatus,
    OutboundMail,
    OutboundMailError,
    OutboundMailStatus,
    WorkspaceInvitation,
)
from pyracast.background.clock import Clock
from pyracast.background.deadlines import ACTORS, PUBLISH_BATCH
from pyracast.background.lease import carries, claimable, expired, release, take
from pyracast.background.retry_policy import MAIL
from pyracast.encryption import secret_cipher
from pyracast.mail.errors import (
    DeliveryUncertainError,
    MailFailure,
    NotSentError,
    PermanentlyNotSentError,
    SendCancelledError,
)
from pyracast.mail.send import send
from pyracast.notifications.event_messages import compose_event_mail

logger = logging.getLogger(__name__)

LEASE_DURATION = ACTORS["send_outbound_mail"].lease
REPUBLISH_AFTER = timedelta(minutes=1)
ANONYMOUS_MAIL_PER_HOUR = 20
# Two-key advisory lock ("PYOB", 1) that serializes the anonymous budget across workers.
ANONYMOUS_BUDGET_LOCK = (0x50594F42, 1)
WAITING = (OutboundMailStatus.PENDING, OutboundMailStatus.RETRY)
UNDELIVERED = (OutboundMailStatus.FAILED, OutboundMailStatus.UNCERTAIN)


@sensitive_variables()
def unseal_code(sealed: str) -> str:
    return secret_cipher(CODE_CONTEXT).decrypt(sealed.encode("ascii")).decode()


def _due(now: datetime) -> Q:
    return Q(status__in=WAITING, available_at__lte=now) | claimable(OutboundMailStatus.SENDING, now)


def pending_mail_ids(now: datetime, limit: int = PUBLISH_BATCH) -> list[int]:
    return list(
        OutboundMail.objects.filter(_due(now))
        .order_by("available_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )


def publish_due_mail(
    publish: Callable[[int], object], now: datetime, limit: int = PUBLISH_BATCH
) -> int:
    """Hand each due intent to the mailer at most once a minute; a lost message is published
    again by a later call."""
    with transaction.atomic():
        ids = list(
            OutboundMail.objects.select_for_update(skip_locked=True)
            .filter(_due(now))
            .filter(Q(published_at__isnull=True) | Q(published_at__lte=now - REPUBLISH_AFTER))
            .order_by("available_at", "pk")
            .values_list("pk", flat=True)[:limit]
        )
        OutboundMail.objects.filter(pk__in=ids).update(published_at=now)
    for mail_id in ids:
        publish(mail_id)
    return len(ids)


def _report(mail: OutboundMail) -> None:
    if mail.status in UNDELIVERED:
        logger.error(
            "outbound_mail_failed mail_id=%s kind=%s status=%s error_code=%s",
            mail.pk,
            mail.kind,
            mail.status,
            mail.last_error,
        )
    elif mail.status == OutboundMailStatus.SKIPPED:
        reason = "budget" if mail.last_error == OutboundMailError.BUDGET_EXHAUSTED else ""
        logger.info(
            "outbound_mail_skipped mail_id=%s kind=%s reason=%s",
            mail.pk,
            mail.kind,
            reason or mail.last_error,
        )


def _finish(mail: OutboundMail, status: OutboundMailStatus, error: str, now: datetime) -> None:
    mail.status, mail.last_error, mail.finished_at, mail.sealed_code = status, error, now, ""
    if status != OutboundMailStatus.UNCERTAIN:
        release(mail)
    mail.save()
    if mail.invitation_id is not None and status in (OutboundMailStatus.SENT, *UNDELIVERED):
        WorkspaceInvitation.objects.filter(pk=mail.invitation_id).update(
            delivery_status=InvitationDeliveryStatus(status)
        )
    _report(mail)


def _retry_or_fail(mail: OutboundMail, now: datetime, error: str, delay: timedelta) -> None:
    retry_at = now + delay
    if not MAIL.exhausted(mail.attempts) and retry_at < mail.expires_at:
        mail.status, mail.last_error, mail.available_at = OutboundMailStatus.RETRY, error, retry_at
        release(mail)
        mail.transmit_started_at = None
        mail.save()
        logger.warning(
            "outbound_mail_retry mail_id=%s kind=%s attempts=%s error_code=%s",
            mail.pk,
            mail.kind,
            mail.attempts,
            error,
        )
    else:
        _finish(mail, OutboundMailStatus.FAILED, error, now)


def _recover(mail: OutboundMail, now: datetime) -> None:
    # The worker that held the lease is gone: after the transmit marker the mail may have left.
    if mail.transmit_started_at is not None:
        _finish(mail, OutboundMailStatus.UNCERTAIN, OutboundMailError.INTERRUPTED_SEND, now)
    else:
        _retry_or_fail(mail, now, OutboundMailError.INTERRUPTED_CONNECTION, timedelta())


def _claim(mail_id: int, now: datetime) -> tuple[OutboundMail, uuid.UUID] | None:
    with transaction.atomic():
        mail = OutboundMail.objects.select_for_update().filter(pk=mail_id).first()
        if mail is None or mail.status not in OPEN_MAIL:
            return None
        if mail.status == OutboundMailStatus.SENDING:
            if expired(mail, now):
                _recover(mail, now)
            return None
        if mail.available_at > now:
            return None
        if mail.expires_at <= now:
            _finish(mail, OutboundMailStatus.SKIPPED, OutboundMailError.EXPIRED, now)
            return None
        token = take(mail, OutboundMailStatus.SENDING, now, LEASE_DURATION)
        mail.transmit_started_at = None
        mail.save()
        return mail, token


def _anonymous_budget_left(now: datetime) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", ANONYMOUS_BUDGET_LOCK)
    sent = OutboundMail.objects.filter(
        kind__in=ANONYMOUS_MAIL, transmit_started_at__gt=now - timedelta(hours=1)
    ).count()
    return sent < ANONYMOUS_MAIL_PER_HOUR


@sensitive_variables()
def _compose(mail: OutboundMail, now: datetime) -> EmailMultiAlternatives | OutboundMailError:
    if mail.invitation_id is not None:
        return compose_invitation(mail.invitation_id, mail.language, now)
    if mail.kind in EVENT_MAIL:
        return compose_event_mail(mail)
    code = unseal_code(mail.sealed_code) if mail.sealed_code else ""
    return compose_account_mail(mail, code)


def _authorize(
    mail_id: int, token: uuid.UUID, clock: Callable[[], datetime], message: EmailMultiAlternatives
) -> bool:
    """Runs on the open connection, just before the mail transaction: re-checks the lease and
    the intent, renders the message in memory and sets the transmit marker."""
    with transaction.atomic():
        mail = OutboundMail.objects.select_for_update().filter(pk=mail_id).first()
        now = clock()
        if mail is None or not carries(mail, token, OutboundMailStatus.SENDING):
            return False
        if expired(mail, now):
            _recover(mail, now)
            return False
        if mail.expires_at <= now:
            _finish(mail, OutboundMailStatus.SKIPPED, OutboundMailError.EXPIRED, now)
            return False
        if mail.kind in ANONYMOUS_MAIL and not _anonymous_budget_left(now):
            _finish(mail, OutboundMailStatus.SKIPPED, OutboundMailError.BUDGET_EXHAUSTED, now)
            return False
        composed = _compose(mail, now)
        if isinstance(composed, OutboundMailError):
            _finish(mail, OutboundMailStatus.SKIPPED, composed, now)
            return False
        message.subject, message.body, message.to = composed.subject, composed.body, composed.to
        message.from_email, message.extra_headers = composed.from_email, composed.extra_headers
        message.alternatives = composed.alternatives
        mail.transmit_started_at = now
        mail.save(update_fields=["transmit_started_at"])
        return True


def _record(
    mail_id: int, token: uuid.UUID, now: datetime, outcome: OutboundMailStatus, error: str
) -> bool:
    with transaction.atomic():
        mail = (
            OutboundMail.objects.select_for_update()
            .filter(
                pk=mail_id,
                lease_token=token,
                status__in=(OutboundMailStatus.SENDING, OutboundMailStatus.UNCERTAIN),
            )
            .first()
        )
        if mail is None:
            return False
        if outcome == OutboundMailStatus.RETRY and mail.status == OutboundMailStatus.UNCERTAIN:
            # Its code was erased when it became uncertain, and an uncertain intent never reopens.
            _finish(mail, OutboundMailStatus.FAILED, error, now)
        elif outcome == OutboundMailStatus.RETRY:
            _retry_or_fail(mail, now, error, MAIL.delay(mail.attempts))
        else:
            _finish(mail, outcome, error, now)
        return outcome == OutboundMailStatus.SENT


def send_outbound_mail(mail_id: int, now: datetime) -> bool:
    clock = Clock(now).now
    claimed = _claim(mail_id, now)
    if claimed is None:
        return False
    mail, token = claimed
    message = EmailMultiAlternatives(to=[mail.recipient])
    transmitting = False

    def before_send() -> bool:
        nonlocal transmitting
        transmitting = _authorize(mail_id, token, clock, message)
        return transmitting

    try:
        send(message, before_send=before_send)
    except SendCancelledError:
        return False
    except PermanentlyNotSentError as error:
        outcome, code = OutboundMailStatus.FAILED, str(error.code)
    except NotSentError as error:
        outcome, code = OutboundMailStatus.RETRY, str(error.code)
    except DeliveryUncertainError:
        outcome, code = OutboundMailStatus.UNCERTAIN, str(MailFailure.OUTCOME_UNCERTAIN)
    except Exception as error:
        logger.warning(
            "outbound_mail_send_failed mail_id=%s kind=%s error_type=%s transmitting=%s",
            mail_id,
            mail.kind,
            type(error).__name__,
            transmitting,
        )
        outcome, code = (
            (OutboundMailStatus.UNCERTAIN, str(MailFailure.OUTCOME_UNCERTAIN))
            if transmitting
            else (OutboundMailStatus.RETRY, str(MailFailure.NOT_SENT))
        )
    else:
        outcome, code = OutboundMailStatus.SENT, ""
    return _record(mail_id, token, clock(), outcome, code)


def end_expired_mail(now: datetime, limit: int = 100) -> int:
    with transaction.atomic():
        expired = list(
            OutboundMail.objects.select_for_update(skip_locked=True)
            .filter(status__in=WAITING, expires_at__lte=now)
            .order_by("pk")[:limit]
        )
        for mail in expired:
            _finish(mail, OutboundMailStatus.SKIPPED, OutboundMailError.EXPIRED, now)
    return len(expired)


def purge_finished_mail(cutoff: datetime, limit: int) -> int:
    with transaction.atomic():
        ids = list(
            OutboundMail.objects.select_for_update(skip_locked=True)
            .exclude(status__in=OPEN_MAIL)
            .filter(finished_at__lt=cutoff)
            .order_by("finished_at", "pk")
            .values_list("pk", flat=True)[:limit]
        )
        return OutboundMail.objects.filter(pk__in=ids).delete()[0]
