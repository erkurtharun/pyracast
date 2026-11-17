import smtplib
from base64 import b64decode, b64encode
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Never, cast

from django.conf import settings
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.utils import translation
from django.utils.translation import gettext as _

from pyracast.mail_delivery import NotSentError as NotSentError
from pyracast.mail_delivery import PermanentlyNotSentError as PermanentlyNotSentError
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.rendering import render_forecast

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class SendCancelledError(Exception):
    """The delivery lost its authority before the mail transaction began."""


def build_message(
    delivery: NotificationDelivery,
    results: list[dict[str, object] | None] | None = None,
    *,
    prepared_at: datetime | None = None,
    report_context: dict[str, object] | None = None,
) -> EmailMultiAlternatives:
    if delivery.prepared_message is not None:
        if delivery.prepared_message.get("format") == "plain":
            return build_unavailable_message(delivery, prepared_at or delivery.scheduled_for)[0]
        return _restore_message(delivery, cast(dict[str, str], delivery.prepared_message))
    with translation.override(delivery.language):
        return _restore_message(
            delivery,
            render_forecast(
                delivery,
                results,
                prepared_at=prepared_at or delivery.scheduled_for,
                report_context=report_context,
            ),
        )


def build_unavailable_message(
    delivery: NotificationDelivery, prepared_at: datetime
) -> tuple[EmailMultiAlternatives, dict[str, str]]:
    existing = delivery.prepared_message
    with translation.override(delivery.language):
        payload = (
            existing
            if isinstance(existing, dict) and existing.get("format") == "plain"
            else {
                "format": "plain",
                "subject": _("Pyracast forecast unavailable"),
                "body": _(
                    "Tomorrow's saved forecast could not be prepared. "
                    "No forecast energy total is available.\n"
                    "Scheduled reminder: %(scheduled)s\n"
                    "Please check the plant page for collection status."
                )
                % {"scheduled": delivery.scheduled_for.isoformat()},
                "kind": "unavailable",
                "prepared_at": prepared_at.isoformat(),
            }
        )
    return EmailMultiAlternatives(
        subject=payload["subject"],
        body=payload["body"],
        from_email=settings.DEFAULT_FROM_EMAIL,
        bcc=delivery.recipients,
        headers={"Message-ID": f"<pyracast-reminder-{delivery.pk}@notifications.pyracast>"},
    ), payload


def _restore_message(
    delivery: NotificationDelivery, payload: dict[str, str]
) -> EmailMultiAlternatives:
    message = EmailMultiAlternatives(
        subject=payload["subject"],
        body=payload["body"],
        from_email=settings.DEFAULT_FROM_EMAIL,
        bcc=delivery.recipients,
        headers={"Message-ID": f"<pyracast-reminder-{delivery.pk}@notifications.pyracast>"},
    )
    message.attach_alternative(payload["html"], "text/html")
    message.attach(
        payload["attachment_name"],
        b64decode(payload["attachment_base64"], validate=True),
        XLSX_MIME,
    )
    return message


def message_payload(message: EmailMultiAlternatives) -> dict[str, str]:
    return {
        "subject": str(message.subject),
        "body": str(message.body),
        "html": cast(str, message.alternatives[0][0]),
        "format": "xlsx",
        "attachment_name": cast(str, message.attachments[0][0]),
        "attachment_base64": b64encode(cast(bytes, message.attachments[0][1])).decode("ascii"),
    }


def send_message(
    message: EmailMultiAlternatives, *, before_send: Callable[[], bool] | None = None
) -> None:
    if not message.recipients():
        raise NotSentError("The message has no recipient.")
    backend = mail.mailers["default"]
    try:
        try:
            backend.fail_silently = False
            backend.open()
        except smtplib.SMTPAuthenticationError:
            raise NotSentError(
                "The mail server did not authenticate the connection.",
                code="mail_authentication_failed",
            ) from None
        except OSError:
            raise NotSentError("The mail connection could not be established.") from None
        if before_send is not None:
            try:
                permitted = before_send()
            except Exception:
                raise NotSentError(
                    "Transmission authority could not be confirmed.",
                    code="mail_preparation_failed",
                ) from None
            if not permitted:
                raise SendCancelledError
        try:
            count = backend.send_messages([message])
        except smtplib.SMTPRecipientsRefused as error:
            codes = [response[0] for response in error.recipients.values()]
            if not codes or any(not 400 <= code < 600 for code in codes):
                _raise_rejection(-1)
            _raise_rejection(450 if any(400 <= code < 500 for code in codes) else 550)
        except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError, smtplib.SMTPHeloError) as error:
            _raise_rejection(error.smtp_code)
        except smtplib.SMTPNotSupportedError:
            raise PermanentlyNotSentError("The mail transaction is unsupported.") from None
        if count != 1:
            raise NotSentError("The mail backend did not send the message.")
    finally:
        # QUIT cannot revoke an already acknowledged DATA response.
        with suppress(OSError):
            backend.close()


def _raise_rejection(code: int) -> Never:
    if not 400 <= code < 600:
        raise smtplib.SMTPException("The mail server returned an invalid transaction response.")
    failure = NotSentError if 400 <= code < 500 else PermanentlyNotSentError
    raise failure("The mail server rejected the message.") from None
