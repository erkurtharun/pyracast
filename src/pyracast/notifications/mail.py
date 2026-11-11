import csv
import io
from datetime import timedelta
from html import escape
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from pyracast.api.public import public_source_data
from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderData
from pyracast.notifications.models import NotificationDelivery


class NotSentError(Exception):
    """The adapter guarantees that the message was not accepted for delivery."""


def build_message(delivery: NotificationDelivery) -> EmailMultiAlternatives:
    snapshot = delivery.snapshot
    assert snapshot is not None  # noqa: S101 -- Internal claimed-delivery invariant.
    binding = snapshot.job.binding
    zone = ZoneInfo(binding.timezone)
    today = delivery.scheduled_for.astimezone(zone).date()
    data = ProviderData(
        snapshot.raw_payload,
        snapshot.normalized_intervals,
        snapshot.issued_at,
        snapshot.collected_at,
        source_kind="forecast",
    )
    lines = [
        f"Forecast for {delivery.preference.region.name}",
        f"Scheduled: {delivery.scheduled_for.astimezone(zone).isoformat()} ({binding.timezone})",
        f"Collected: {snapshot.collected_at.isoformat()}",
        "Provider issue: "
        + (snapshot.issued_at.isoformat() if snapshot.issued_at else "unavailable"),
        f"Saved forecast age at reminder: {delivery.scheduled_for - snapshot.collected_at}",
        "Stale saved forecast (older than 24 hours)."
        if delivery.scheduled_for - snapshot.collected_at > timedelta(hours=24)
        else "Saved forecast is at most 24 hours old.",
        f"Binding revision: {binding.pk}:{binding.region_revision}",
        "Dataset kind: forecast",
        "Unvalidated irradiance proxy. This is incident solar energy, not plant electricity.",
        "Latest saved forecast at the scheduled time. No new provider request was made.",
    ]
    csv_file = io.StringIO(newline="")
    writer = csv.writer(csv_file)
    writer.writerow(("local_date", "start", "end", "ghi_wm2", "quality_flags"))
    html_sections = [
        '<!doctype html><html lang="en"><body style="font-family:Arial,sans-serif;color:#162133">',
        f"<h1>{escape(lines[0])}</h1>",
        *(f"<p>{escape(line)}</p>" for line in lines[1:]),
    ]
    for day in (today, today + timedelta(days=1)):
        result = public_source_data(calculate(binding, day, data), technical=True)
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
    body = "\n".join(lines)
    message = EmailMultiAlternatives(
        subject=f"Pyracast forecast — {today}",
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[delivery.recipient],
        headers={"Message-ID": f"<pyracast-reminder-{delivery.pk}@notifications.pyracast>"},
    )
    message.attach_alternative(
        "".join(html_sections)
        + "<p>Interval values and quality flags are attached as CSV.</p></body></html>",
        "text/html",
    )
    message.attach(f"irradiance-{today}.csv", csv_file.getvalue(), "text/csv")
    return message


def send_message(message: EmailMultiAlternatives) -> None:
    if message.send() != 1:
        raise NotSentError("The mail backend did not send the message.")
