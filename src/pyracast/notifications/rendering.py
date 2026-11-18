from base64 import b64encode
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

from django.template.loader import render_to_string
from django.utils.formats import date_format, number_format
from django.utils.translation import gettext as _

from pyracast.notifications.models import NotificationDelivery, PreparedMessageFormat
from pyracast.notifications.presentation import present_report, source_label
from pyracast.notifications.report_data import saved_report_context
from pyracast.notifications.sources import saved_report_results
from pyracast.notifications.workbook import build_workbook


def energy_text(total: float | None, partial: float, coverage: float | None, unit: str) -> str:
    if total is not None:
        return f"{number_format(total, decimal_pos=3)} {unit}"
    if not coverage:
        return _("Unavailable")
    return _("Partial: %(energy)s %(unit)s (%(coverage)s%% coverage)") % {
        "energy": number_format(partial, decimal_pos=3),
        "unit": unit,
        "coverage": number_format(coverage * 100, decimal_pos=0),
    }


def render_forecast(
    delivery: NotificationDelivery,
    results: list[dict[str, object] | None] | None,
    *,
    prepared_at: datetime,
    report_context: dict[str, object] | None,
) -> dict[str, str]:
    context = report_context if report_context is not None else saved_report_context(delivery)
    zone = ZoneInfo(cast(str, context["timezone"]))
    if results is None:
        results = saved_report_results(delivery, delivery.scheduled_for)
    report = present_report(
        context,
        results,
        prepared_at=prepared_at,
        fallback=bool(
            delivery.snapshot and delivery.snapshot.job.scheduled_for != delivery.scheduled_for
        ),
    )
    has_outage = any(day.has_outage for day in report.days)
    normal_label = _("Normal operation") if has_outage else _("Forecast")
    outage_label = _("With reported outage")
    title = _("Forecast for %(plant)s") % {"plant": report.plant_name}
    rows = [
        {
            "date": date_format(day.day, "j M Y"),
            "label": source_label(day.source_kind) if not has_outage else normal_label,
            "source": source_label(day.source_kind)
            if has_outage and day.source_kind != "forecast"
            else "",
            "normal": energy_text(day.total, day.partial, day.coverage, report.unit),
            "outage": energy_text(
                day.outage_total, day.outage_partial, day.outage_coverage, report.unit
            )
            if day.has_outage
            else "",
        }
        for day in reversed(report.days)
    ]
    updated = (
        _("Updated: %(time)s (%(timezone)s)")
        % {
            "time": date_format(report.collected_at.astimezone(zone), "j M Y H:i"),
            "timezone": report.timezone,
        }
        if report.collected_at
        else _("Tomorrow's forecast is unavailable.")
    )
    subject = _("%(plant)s — forecast for %(date)s") % {
        "plant": " ".join(report.plant_name.split()),
        "date": date_format(report.days[-1].day, "j M Y"),
    }
    template_context = {
        "title": title,
        "subject": subject,
        "rows": rows,
        "normal_label": normal_label,
        "outage_label": outage_label,
        "updated": updated,
        "warnings": report.warnings,
        "url": report.url,
    }
    return {
        "format": PreparedMessageFormat.XLSX,
        "subject": subject,
        "body": render_to_string("notifications/forecast.txt", template_context).strip(),
        "html": render_to_string("notifications/forecast.html", template_context),
        "attachment_name": f"forecast-{report.days[-1].day.isoformat()}.xlsx",
        "attachment_base64": b64encode(build_workbook(report)).decode("ascii"),
    }
