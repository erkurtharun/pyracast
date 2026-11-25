from base64 import b64encode
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from pyracast.notifications.formats import (
    day_text,
    energy_number,
    file_name,
    long_day,
    moment_text,
    subject_day,
    utc_offset,
    with_edition,
)
from pyracast.notifications.links import forecast_day_url
from pyracast.notifications.models import NotificationDelivery, PreparedMessageFormat
from pyracast.notifications.presentation import (
    Figure,
    ForecastReport,
    ReportDay,
    figure,
    present_report,
)
from pyracast.notifications.report_data import saved_report_context
from pyracast.notifications.sources import saved_report_results
from pyracast.notifications.workbook import build_workbook

NBSP = "\u00a0"


@dataclass(frozen=True)
class ForecastMail:
    footer: str
    plant_url: str
    edition: str | None = None
    missed_collection: bool = False


def kind_line(electrical: bool) -> str:
    if electrical:
        return _("Generation forecast")
    return _("Solar irradiance forecast (GHI), not electricity generation")


def text_heading(plant: str, electrical: bool) -> str:
    if electrical:
        return _("%(plant)s: generation forecast") % {"plant": plant}
    return _("%(plant)s: solar irradiance forecast (GHI), not electricity generation") % {
        "plant": plant
    }


def no_forecast_subject(plant: str, day: date, edition: str | None) -> str:
    return with_edition(
        _("%(plant)s: no forecast for %(date)s")
        % {"plant": " ".join(plant.split()), "date": subject_day(day)},
        edition,
    )


def no_forecast_sentence(day: date) -> str:
    return _("The forecast for %(date)s could not be prepared. This email contains no figures.") % {
        "date": long_day(day)
    }


def _subject(report: ForecastReport, shown: Figure, edition: str | None) -> str:
    tomorrow = report.days[-1]
    if shown.gap == "missing":
        return no_forecast_subject(report.plant_name, tomorrow.day, edition)
    values = {"plant": " ".join(report.plant_name.split()), "date": subject_day(tomorrow.day)}
    if shown.gap == "incomplete":
        subject = _("%(plant)s: forecast for %(date)s is incomplete")
    elif report.electrical:
        subject = _("%(plant)s: forecast for %(date)s")
    else:
        subject = _("%(plant)s: irradiance forecast for %(date)s")
    return with_edition(subject % values, edition)


def _amount(value: float, unit: str, space: str) -> str:
    return f"{energy_number(value)}{space}{unit}"


def _notes(shown: Figure, unit: str, space: str) -> list[str]:
    if shown.outage == "included":
        return [
            _("Includes the recorded outage. Without it: %(energy)s.")
            % {"energy": _amount(shown.without_outage or 0, unit, space)}
        ]
    if shown.outage in ("excluded", "unknown"):
        notes = [_("The recorded outage is not included in this figure.")]
        if shown.outage == "unknown":
            notes.append(_("Enter the available capacity in the outage record to include it."))
        return [" ".join(notes)]
    return []


def _gap(shown: Figure) -> str:
    if shown.gap == "incomplete":
        return _("Data is missing for some hours, so no daily total is given.")
    return _("No forecast data for this day.")


def _day(day: ReportDay, word: str, unit: str) -> dict[str, object]:
    shown = figure(day)
    return {
        "word": word,
        "date": day_text(day.day),
        "value": "" if shown.value is None else energy_number(shown.value),
        "gap": "" if shown.value is not None else _gap(shown),
        "notes": _notes(shown, unit, NBSP),
        "text_notes": _notes(shown, unit, " "),
    }


def _preheader(report: ForecastReport) -> str:
    today, tomorrow = (figure(day) for day in report.days)
    if tomorrow.value is not None:
        parts = [f"{_('Tomorrow')} {_amount(tomorrow.value, report.unit, NBSP)}"]
    elif tomorrow.gap == "incomplete":
        parts = [_("Tomorrow's forecast is incomplete")]
    else:
        parts = [_("No forecast for tomorrow")]
    if today.value is not None:
        parts.append(f"{_('Today')} {_amount(today.value, report.unit, NBSP)}")
    return " · ".join(parts)


def _data_line(report: ForecastReport) -> str:
    if report.collected_at is None:
        return ""
    local = report.collected_at.astimezone(ZoneInfo(report.timezone))
    return _("Forecast data: %(time)s (%(offset)s)") % {
        "time": moment_text(local),
        "offset": utc_offset(local),
    }


def render_no_forecast(
    plant: str, tomorrow: date, mail: ForecastMail, *, kind: str, heading: str
) -> dict[str, str]:
    context = {
        "subject": no_forecast_subject(plant, tomorrow, mail.edition),
        "heading": heading,
        "kind": kind,
        "plant": plant,
        "preheader": _("No forecast for tomorrow"),
        "no_data": no_forecast_sentence(tomorrow),
        "action_label": _("Open plant in Pyracast"),
        "url": mail.plant_url,
        "footer": mail.footer,
    }
    return {
        "subject": str(context["subject"]),
        "body": render_to_string("notifications/forecast.txt", context).strip(),
        "html": render_to_string("notifications/forecast.html", context),
    }


def render_forecast(
    delivery: NotificationDelivery,
    results: list[dict[str, object] | None] | None,
    *,
    prepared_at: datetime,
    report_context: dict[str, object] | None,
    mail: ForecastMail,
) -> dict[str, str]:
    context = report_context if report_context is not None else saved_report_context(delivery)
    if results is None:
        results = saved_report_results(delivery, delivery.scheduled_for)
    report = present_report(
        context, results, prepared_at=prepared_at, missed_collection=mail.missed_collection
    )
    today, tomorrow = report.days
    heading = text_heading(report.plant_name, report.electrical)
    if all(figure(day).gap == "missing" for day in report.days):
        rendered = render_no_forecast(
            report.plant_name,
            tomorrow.day,
            mail,
            kind=kind_line(report.electrical),
            heading=heading,
        )
        return {"format": PreparedMessageFormat.XLSX, **rendered}
    subject = _subject(report, figure(tomorrow), mail.edition)
    days = [_day(tomorrow, _("Tomorrow"), report.unit), _day(today, _("Today"), report.unit)]
    template_context = {
        "subject": subject,
        "heading": heading,
        "kind": kind_line(report.electrical),
        "plant": report.plant_name,
        "preheader": _preheader(report),
        "days": days,
        "unit": report.unit,
        "stale": _("Newer forecast data was not available.") if report.stale else "",
        "data_line": _data_line(report),
        "attachment_line": _("Hourly values are in the attached Excel file.")
        if report.electrical
        else _("Interval values are in the attached Excel file."),
        "action_label": _("Open forecast in Pyracast"),
        "url": forecast_day_url(mail.plant_url, tomorrow.day, irradiance=not report.electrical),
        "footer": mail.footer,
    }
    return {
        "format": PreparedMessageFormat.XLSX,
        "subject": subject,
        "body": render_to_string("notifications/forecast.txt", template_context).strip(),
        "html": render_to_string("notifications/forecast.html", template_context),
        "attachment_name": file_name(report.plant_name, tomorrow.day, mail.edition),
        "attachment_base64": b64encode(build_workbook(report)).decode("ascii"),
    }
