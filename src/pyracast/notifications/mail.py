import csv
import io
import smtplib
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from html import escape
from typing import Never, cast
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core import mail
from django.core.mail import EmailMultiAlternatives

from pyracast.api.public import public_source_data
from pyracast.forecasts.projections import saved_day_payload
from pyracast.notifications.models import NotificationDelivery


class NotSentError(Exception):
    """The adapter guarantees that the message was not accepted for delivery."""

    def __init__(self, message: str = "", *, code: str = "mail_not_sent") -> None:
        super().__init__(message)
        self.code = code


class PermanentlyNotSentError(NotSentError):
    """The mail server rejected delivery without a retryable response."""


class SendCancelledError(Exception):
    """The delivery lost its authority before the mail transaction began."""


def build_message(
    delivery: NotificationDelivery,
    results: list[dict[str, object] | None] | None = None,
    *,
    prepared_at: datetime | None = None,
) -> EmailMultiAlternatives:
    if delivery.prepared_message is not None:
        return _restore_message(delivery, cast(dict[str, str], delivery.prepared_message))
    snapshot = delivery.snapshot
    binding = delivery.binding
    assert binding is not None  # noqa: S101 -- Internal authorized-delivery invariant.
    zone = ZoneInfo(binding.timezone)
    today = delivery.scheduled_for.astimezone(zone).date()
    prepared_at = prepared_at or delivery.scheduled_for
    lines = [
        f"Forecast for {delivery.preference.region.name}",
        f"Scheduled: {delivery.scheduled_for.astimezone(zone).isoformat()} ({binding.timezone})",
        f"Collected: {snapshot.collected_at.isoformat()}" if snapshot else "Collected: unavailable",
        "Provider issue: "
        + (snapshot.issued_at.isoformat() if snapshot and snapshot.issued_at else "unavailable"),
        "Saved forecast age at preparation: "
        f"{max(prepared_at - snapshot.collected_at, timedelta())}"
        if snapshot
        else "No usable saved forecast is available. Tomorrow's forecast is unavailable.",
        "Stale saved forecast (older than 24 hours)."
        if snapshot and prepared_at - snapshot.collected_at > timedelta(hours=24)
        else "Saved forecast is at most 24 hours old."
        if snapshot
        else "Source freshness: missing.",
        "Previously saved forecast fallback; freshness is reduced. Check the collection timestamp."
        if snapshot and snapshot.job.scheduled_slot != delivery.scheduled_for
        else "Scheduled collection forecast."
        if snapshot
        else "No daily energy total is available.",
        "Dataset kind: forecast",
        "Unvalidated irradiance proxy. This is incident solar energy, not plant electricity.",
        "Saved forecast selected within the reminder preparation deadline. "
        "No new provider request was made.",
    ]
    csv_file = io.StringIO(newline="")
    writer = csv.writer(csv_file)
    writer.writerow(("local_date", "start", "end", "ghi_wm2", "quality_flags"))
    html_sections = [
        '<!doctype html><html lang="en"><body style="font-family:Arial,sans-serif;color:#162133">',
        f"<h1>{escape(lines[0])}</h1>",
        *(f"<p>{escape(line)}</p>" for line in lines[1:]),
    ]
    days = (today, today + timedelta(days=1))
    if results is None:
        results = [saved_day_payload(snapshot, day) if snapshot else None for day in days]
    for day, saved_result in zip(days, results, strict=True):
        if saved_result is None:
            lines.extend(
                (
                    "",
                    f"Date: {day} ({binding.timezone})",
                    "Daily energy: Unavailable (no usable saved coverage)",
                )
            )
            html_sections.extend(
                (
                    f"<h2>{day} — {escape(binding.timezone)}</h2>",
                    "<p>Daily energy: Unavailable (no usable saved coverage)</p>",
                )
            )
            continue
        result = public_source_data(saved_result, technical=True)
        total = result["energy_kwh_m2"]
        total_text = "Unavailable (incomplete coverage)" if total is None else f"{total:.3f} kWh/m²"
        lines.extend(
            (
                "",
                f"Date: {day} ({binding.timezone})",
                f"Complete: {result['complete']}; coverage fraction: {result['coverage_fraction']}",
                f"Daily energy: {total_text}",
                f"Available partial energy: {result['partial_energy_kwh_m2']} kWh/m²",
                f"Model: {result['model_version']}; quality: {result['quality_flags']}",
                "Interval mean irradiance (W/m²) and quality flags are in the attached CSV.",
            )
        )
        html_sections.extend(
            (
                f"<h2>{day} — {escape(binding.timezone)}</h2>",
                '<table style="border-collapse:collapse" aria-label="Daily irradiance summary">',
                f'<tr><th scope="row">Daily energy</th><td>{escape(total_text)}</td></tr>',
                '<tr><th scope="row">Available partial energy</th>'
                f"<td>{result['partial_energy_kwh_m2']} kWh/m²</td></tr>",
                f'<tr><th scope="row">Complete coverage</th><td>{result["complete"]}</td></tr>',
                '<tr><th scope="row">Coverage fraction</th>'
                f"<td>{result['coverage_fraction']}</td></tr>",
                "</table>",
                f"<p>Model: {escape(str(result['model_version']))}; "
                f"quality: {escape(str(result['quality_flags']))}</p>",
            )
        )
        intervals = result["intervals"]
        assert isinstance(intervals, list)  # noqa: S101 -- calculate's serialized contract.
        for row in intervals:
            writer.writerow(
                (day, row["start"], row["end"], row["ghi_wm2"], ";".join(row["quality_flags"]))
            )
    return _restore_message(
        delivery,
        {
            "subject": f"Pyracast forecast — {today + timedelta(days=1)}",
            "body": "\n".join(lines),
            "html": "".join(html_sections)
            + "<p>Interval values and quality flags are attached as CSV.</p></body></html>",
            "csv_filename": f"irradiance-{today}.csv",
            "csv": csv_file.getvalue(),
        },
    )


def _restore_message(
    delivery: NotificationDelivery, payload: dict[str, str]
) -> EmailMultiAlternatives:
    message = EmailMultiAlternatives(
        subject=payload["subject"],
        body=payload["body"],
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[delivery.recipient],
        headers={"Message-ID": f"<pyracast-reminder-{delivery.pk}@notifications.pyracast>"},
    )
    message.attach_alternative(payload["html"], "text/html")
    message.attach(payload["csv_filename"], payload["csv"], "text/csv")
    return message


def message_payload(message: EmailMultiAlternatives) -> dict[str, str]:
    return {
        "subject": str(message.subject),
        "body": str(message.body),
        "html": cast(str, message.alternatives[0][0]),
        "csv_filename": cast(str, message.attachments[0][0]),
        "csv": cast(str, message.attachments[0][1]),
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
    failure = NotSentError if 400 <= code < 500 else PermanentlyNotSentError
    raise failure("The mail server rejected the message.") from None
