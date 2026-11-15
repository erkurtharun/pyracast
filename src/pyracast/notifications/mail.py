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
from django.utils import translation
from django.utils.formats import number_format
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from pyracast.api.public import public_source_data
from pyracast.forecasts.projections import saved_day_payload
from pyracast.mail_delivery import NotSentError as NotSentError
from pyracast.mail_delivery import PermanentlyNotSentError as PermanentlyNotSentError
from pyracast.notifications.models import NotificationDelivery


class SendCancelledError(Exception):
    """The delivery lost its authority before the mail transaction began."""


def _age_text(age: timedelta) -> str:
    if not age.days:
        return str(age)
    return ngettext("%(days)d day, %(time)s", "%(days)d days, %(time)s", age.days) % {
        "days": age.days,
        "time": age - timedelta(days=age.days),
    }


def build_message(
    delivery: NotificationDelivery,
    results: list[dict[str, object] | None] | None = None,
    *,
    prepared_at: datetime | None = None,
) -> EmailMultiAlternatives:
    if delivery.prepared_message is not None:
        if delivery.prepared_message.get("format") == "plain":
            return build_unavailable_message(delivery, prepared_at or delivery.scheduled_for)[0]
        return _restore_message(delivery, cast(dict[str, str], delivery.prepared_message))
    with translation.override(delivery.language):
        return _render_message(delivery, results, prepared_at=prepared_at)


def _render_message(
    delivery: NotificationDelivery,
    results: list[dict[str, object] | None] | None,
    *,
    prepared_at: datetime | None,
) -> EmailMultiAlternatives:
    snapshot = delivery.snapshot
    binding = delivery.binding
    assert binding is not None  # noqa: S101 -- Internal authorized-delivery invariant.
    zone = ZoneInfo(binding.timezone)
    today = delivery.scheduled_for.astimezone(zone).date()
    prepared_at = prepared_at or delivery.scheduled_for
    lines = [
        _("Forecast for %(plant)s") % {"plant": delivery.schedule.region.name},
        _("Scheduled: %(scheduled)s (%(timezone)s)")
        % {
            "scheduled": delivery.scheduled_for.astimezone(zone).isoformat(),
            "timezone": binding.timezone,
        },
        _("Collected: %(collected)s") % {"collected": snapshot.collected_at.isoformat()}
        if snapshot
        else _("Collected: unavailable"),
        _("Provider issue: %(issued)s")
        % {
            "issued": snapshot.issued_at.isoformat()
            if snapshot and snapshot.issued_at
            else _("unavailable")
        },
        _("Saved forecast age at preparation: %(age)s")
        % {"age": _age_text(max(prepared_at - snapshot.collected_at, timedelta()))}
        if snapshot
        else _("No usable saved forecast is available. Tomorrow's forecast is unavailable."),
        _("Stale saved forecast (older than 24 hours).")
        if snapshot and prepared_at - snapshot.collected_at > timedelta(hours=24)
        else _("Saved forecast is at most 24 hours old.")
        if snapshot
        else _("Source freshness: missing."),
        _(
            "Previously saved forecast fallback; freshness is reduced. "
            "Check the collection timestamp."
        )
        if snapshot and snapshot.job.scheduled_slot != delivery.scheduled_for
        else _("Scheduled collection forecast.")
        if snapshot
        else _("No daily energy total is available."),
        _("Dataset kind: forecast"),
        _("Unvalidated irradiance proxy. This is incident solar energy, not plant electricity."),
        _(
            "Saved forecast selected within the reminder preparation deadline. "
            "No new provider request was made."
        ),
    ]
    csv_file = io.StringIO(newline="")
    writer = csv.writer(csv_file)
    writer.writerow(("local_date", "start", "end", "ghi_wm2", "quality_flags"))
    html_sections = [
        f'<!doctype html><html lang="{delivery.language}">'
        '<body style="font-family:Arial,sans-serif;color:#162133">',
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
                    _("Date: %(date)s (%(timezone)s)")
                    % {"date": day, "timezone": binding.timezone},
                    _("Daily energy: Unavailable (no usable saved coverage)"),
                )
            )
            html_sections.extend(
                (
                    f"<h2>{day} — {escape(binding.timezone)}</h2>",
                    f"<p>{escape(_('Daily energy: Unavailable (no usable saved coverage)'))}</p>",
                )
            )
            continue
        result = public_source_data(saved_result, technical=True)
        total = result["energy_kwh_m2"]
        total_text = (
            _("Unavailable (incomplete coverage)")
            if total is None
            else f"{number_format(f'{cast(float, total):.3f}', decimal_pos=3)} kWh/m²"
        )
        complete = _("True") if result["complete"] else _("False")
        coverage = number_format(cast(float, result["coverage_fraction"]))
        partial = number_format(cast(float, result["partial_energy_kwh_m2"]))
        quality = str([_(flag) for flag in cast(list[str], result["quality_flags"])])
        model_quality = _("Model: %(model)s; quality: %(quality)s") % {
            "model": result["model_version"],
            "quality": quality,
        }
        lines.extend(
            (
                "",
                _("Date: %(date)s (%(timezone)s)") % {"date": day, "timezone": binding.timezone},
                _("Complete: %(complete)s; coverage fraction: %(coverage)s")
                % {"complete": complete, "coverage": coverage},
                _("Daily energy: %(energy)s") % {"energy": total_text},
                _("Available partial energy: %(energy)s kWh/m²") % {"energy": partial},
                model_quality,
                _("Interval mean irradiance (W/m²) and quality flags are in the attached CSV."),
            )
        )
        html_sections.extend(
            (
                f"<h2>{day} — {escape(binding.timezone)}</h2>",
                '<table style="border-collapse:collapse" '
                f'aria-label="{escape(_("Daily irradiance summary"))}">',
                f'<tr><th scope="row">{escape(_("Daily energy"))}</th>'
                f"<td>{escape(total_text)}</td></tr>",
                f'<tr><th scope="row">{escape(_("Available partial energy"))}</th>'
                f"<td>{escape(partial)} kWh/m²</td></tr>",
                f'<tr><th scope="row">{escape(_("Complete coverage"))}</th>'
                f"<td>{escape(complete)}</td></tr>",
                f'<tr><th scope="row">{escape(_("Coverage fraction"))}</th>'
                f"<td>{escape(coverage)}</td></tr>",
                "</table>",
                f"<p>{escape(model_quality)}</p>",
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
            "subject": _("Pyracast forecast — %(date)s") % {"date": today + timedelta(days=1)},
            "body": "\n".join(lines),
            "html": "".join(html_sections)
            + f"<p>{escape(_('Interval values and quality flags are attached as CSV.'))}</p>"
            "</body></html>",
            "csv_filename": f"irradiance-{today}.csv",
            "csv": csv_file.getvalue(),
        },
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
        to=[delivery.recipient],
        headers={"Message-ID": f"<pyracast-reminder-{delivery.pk}@notifications.pyracast>"},
    ), payload


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
