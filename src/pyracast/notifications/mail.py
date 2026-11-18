from base64 import b64decode, b64encode
from datetime import datetime
from email.utils import parseaddr
from typing import cast

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import translation
from django.utils.translation import gettext as _

from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.rendering import render_forecast

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def message_id(delivery: NotificationDelivery) -> str:
    domain = parseaddr(settings.DEFAULT_FROM_EMAIL)[1].rpartition("@")[2]
    return f"<reminder.{delivery.message_token}@{domain}>"


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
        headers={"Message-ID": message_id(delivery)},
    ), payload


def _restore_message(
    delivery: NotificationDelivery, payload: dict[str, str]
) -> EmailMultiAlternatives:
    message = EmailMultiAlternatives(
        subject=payload["subject"],
        body=payload["body"],
        from_email=settings.DEFAULT_FROM_EMAIL,
        bcc=delivery.recipients,
        headers={"Message-ID": message_id(delivery)},
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
