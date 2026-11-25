from base64 import b64decode, b64encode
from datetime import datetime
from email.utils import parseaddr
from typing import cast

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import translation

from pyracast.notifications.models import NotificationDelivery, PreparedMessageFormat
from pyracast.notifications.reminder_notice import (
    forecast_mail,
    reminder_headers,
    unavailable_text,
)
from pyracast.notifications.rendering import render_forecast

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def message_id(delivery: NotificationDelivery) -> str:
    domain = parseaddr(settings.DEFAULT_FROM_EMAIL)[1].rpartition("@")[2]
    return f"<reminder.{delivery.message_token}@{domain}>"


def _headers(delivery: NotificationDelivery) -> dict[str, str]:
    return {"Message-ID": message_id(delivery), **reminder_headers(delivery.language)}


def build_message(
    delivery: NotificationDelivery,
    results: list[dict[str, object] | None] | None = None,
    *,
    prepared_at: datetime | None = None,
    report_context: dict[str, object] | None = None,
) -> EmailMultiAlternatives:
    if delivery.prepared_message is not None:
        if delivery.prepared_message.get("format") == PreparedMessageFormat.PLAIN:
            return build_unavailable_message(delivery)[0]
        return _restore_message(delivery, cast(dict[str, str], delivery.prepared_message))
    with translation.override(delivery.language):
        return _restore_message(
            delivery,
            render_forecast(
                delivery,
                results,
                prepared_at=prepared_at or delivery.scheduled_for,
                report_context=report_context,
                mail=forecast_mail(delivery),
            ),
        )


def build_unavailable_message(
    delivery: NotificationDelivery,
) -> tuple[EmailMultiAlternatives, dict[str, str]]:
    existing = delivery.prepared_message
    if isinstance(existing, dict) and existing.get("format") == PreparedMessageFormat.PLAIN:
        payload = existing
    else:
        with translation.override(delivery.language):
            subject, body = unavailable_text(delivery)
        payload = {"format": PreparedMessageFormat.PLAIN, "subject": subject, "body": body}
    return EmailMultiAlternatives(
        subject=payload["subject"],
        body=payload["body"],
        from_email=settings.DEFAULT_FROM_EMAIL,
        headers=_headers(delivery),
    ), payload


def _restore_message(
    delivery: NotificationDelivery, payload: dict[str, str]
) -> EmailMultiAlternatives:
    message = EmailMultiAlternatives(
        subject=payload["subject"],
        body=payload["body"],
        from_email=settings.DEFAULT_FROM_EMAIL,
        headers=_headers(delivery),
    )
    message.attach_alternative(payload["html"], "text/html")
    if "attachment_base64" in payload:
        message.attach(
            payload["attachment_name"],
            b64decode(payload["attachment_base64"], validate=True),
            XLSX_MIME,
        )
    return message


def message_payload(message: EmailMultiAlternatives) -> dict[str, str]:
    payload = {
        "subject": str(message.subject),
        "body": str(message.body),
        "html": cast(str, message.alternatives[0][0]),
        "format": PreparedMessageFormat.XLSX,
    }
    for name, content, _mimetype in message.attachments:
        payload["attachment_name"] = cast(str, name)
        payload["attachment_base64"] = b64encode(cast(bytes, content)).decode("ascii")
    return payload
