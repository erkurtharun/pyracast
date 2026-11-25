from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from io import BytesIO
from itertools import zip_longest
from textwrap import wrap
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from django.utils.translation import gettext as _
from django.utils.translation import pgettext
from openpyxl import Workbook
from openpyxl.cell.cell import Cell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.worksheet import Worksheet

from pyracast.irradiance import local_day_bounds
from pyracast.notifications.formats import day_text, utc_offset
from pyracast.notifications.presentation import Figure, figure, interval_value

if TYPE_CHECKING:
    from pyracast.notifications.presentation import ForecastReport

_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_HEADER = "09383A"
_LEFT = Alignment(horizontal="left", vertical="top")
_RIGHT = Alignment(horizontal="right", vertical="top")
_TOTAL = "#,##0.00"
_INTERVAL = "#,##0.000"
_TITLE_CHARACTERS = 15
_TITLE_LINE_POINTS = 19


def _text(sheet: Worksheet, row: int, column: int, value: str) -> Cell:
    cell = cast(Cell, sheet.cell(row, column))
    cell.value = _ILLEGAL_XML.sub("", value)
    cell.data_type = "s"
    cell.alignment = _LEFT
    return cell


def _number(
    sheet: Worksheet, row: int, column: int, value: float | date | datetime | None, shown: str
) -> None:
    cell = sheet.cell(row, column, value)
    cell.number_format = shown
    cell.alignment = _RIGHT


def _header(sheet: Worksheet, row: int, titles: list[str]) -> None:
    for column, title in enumerate(titles, 1):
        cell = _text(sheet, row, column, title)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=_HEADER)


def _heading(sheet: Worksheet, row: int, title: str) -> None:
    _text(sheet, row, 1, title).font = Font(bold=True, size=12)


def _print_setup(sheet: Worksheet) -> None:
    sheet.sheet_view.showGridLines = True
    sheet.sheet_view.showRowColHeaders = True
    sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0


def _value_label(report: ForecastReport) -> str:
    return _("Forecast (MWh)") if report.electrical else _("Irradiance (kWh/m²)")


def _notes(report: ForecastReport, figures: list[Figure], outage: bool) -> list[str]:
    notes = [_("Newer forecast data was not available.")] if report.stale else []
    if outage:
        notes.append(_("The Forecast column includes the recorded outage."))
    for day, shown in zip(report.days, figures, strict=True):
        values = {"date": day_text(day.day)}
        if shown.gap == "missing":
            notes.append(_("%(date)s: no forecast data.") % values)
        elif shown.gap == "incomplete":
            notes.append(_("%(date)s: data is missing for some hours; no daily total.") % values)
        elif shown.outage in ("excluded", "unknown"):
            notes.append(_("%(date)s: the recorded outage is not included.") % values)
    return notes


def _daily(
    sheet: Worksheet,
    report: ForecastReport,
    figures: list[Figure],
    *,
    outage: bool,
    day_format: str,
) -> int:
    title = _text(sheet, 1, 1, report.plant_name)
    title.font = Font(bold=True, size=14)
    title.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    lines = sum(
        len(wrap(line, _TITLE_CHARACTERS)) or 1 for line in cast(str, title.value).split("\n")
    )
    sheet.row_dimensions[1].height = min(409, lines * _TITLE_LINE_POINTS + 4)
    _text(
        sheet,
        2,
        1,
        _("Generation forecast · Pyracast")
        if report.electrical
        else _("Solar irradiance forecast (GHI) · Pyracast"),
    )
    zone = ZoneInfo(report.timezone)
    collected = report.collected_at.astimezone(zone) if report.collected_at else None
    reference = collected or datetime.combine(report.days[-1].day, datetime.min.time(), zone)
    _text(sheet, 4, 1, _("Forecast data"))
    _number(
        sheet, 4, 2, collected.replace(tzinfo=None) if collected else None, f"{day_format} hh:mm"
    )
    _text(sheet, 5, 1, _("Time zone"))
    _text(sheet, 5, 2, f"{report.timezone} ({utc_offset(reference)})")
    _text(sheet, 6, 1, _("Unit"))
    _text(sheet, 6, 2, report.unit)
    _heading(sheet, 8, _("Daily totals"))
    titles = [_("Date"), _value_label(report)]
    if outage:
        titles.append(_("Without outage (MWh)"))
    _header(sheet, 9, titles)
    row = 9
    for day, shown in zip(report.days, figures, strict=True):
        row += 1
        _number(sheet, row, 1, day.day, day_format)
        _number(sheet, row, 2, shown.value, _TOTAL)
        if outage:
            without = shown.without_outage if shown.outage == "included" else shown.value
            _number(sheet, row, 3, without, _TOTAL)
    for note in _notes(report, figures, outage):
        row += 1
        _text(sheet, row, 1, note)
    return row


def _intervals(
    sheet: Worksheet,
    report: ForecastReport,
    figures: list[Figure],
    *,
    after: int,
    outage: bool,
    day_format: str,
) -> list[int]:
    zone = ZoneInfo(report.timezone)
    rows = [
        (day, shown, item)
        for day, shown in zip(report.days, figures, strict=True)
        for item in day.intervals
    ]
    offsets = any(
        end - start != timedelta(days=1)
        for start, end in (local_day_bounds(day.day, report.timezone) for day in report.days)
    )
    titles = [_("Date"), _("From"), _("To"), _value_label(report)]
    if outage:
        titles.append(_("Without outage (MWh)"))
    if offsets:
        titles.append(_("UTC offset"))
    _heading(sheet, after + 2, _("Hourly values") if report.electrical else _("Interval values"))
    header = after + 3
    _header(sheet, header, titles)
    for row, (day, shown, item) in enumerate(rows, header + 1):
        _number(sheet, row, 1, day.day, day_format)
        _number(sheet, row, 2, item.start.astimezone(zone).replace(tzinfo=None), "hh:mm")
        _number(sheet, row, 3, item.end.astimezone(zone).replace(tzinfo=None), "hh:mm")
        _number(sheet, row, 4, interval_value(item, shown), _INTERVAL)
        column = 5
        if outage:
            _number(sheet, row, column, item.energy if item.complete else None, _INTERVAL)
            column += 1
        if offsets:
            _text(sheet, row, column, utc_offset(item.start.astimezone(zone)))
    sheet.auto_filter.ref = f"A{header}:{get_column_letter(len(titles))}{header + len(rows)}"
    return [14, 8, 8, 20, *((24,) if outage else ()), *((12,) if offsets else ())]


def build_workbook(report: ForecastReport) -> bytes:
    figures = [figure(day) for day in report.days]
    outage = any(shown.outage == "included" for shown in figures)
    day_format = pgettext("Excel date format", "dd mmm yyyy")
    workbook = Workbook()
    workbook.properties.creator = "Pyracast"
    workbook.properties.lastModifiedBy = "Pyracast"
    workbook.properties.title = _ILLEGAL_XML.sub(
        "", f"{report.plant_name} {day_text(report.days[-1].day)}"
    )
    sheet = cast(Worksheet, workbook.active)
    sheet.title = (
        pgettext("workbook sheet", "Forecast")
        if report.electrical
        else pgettext("workbook sheet", "Irradiance")
    )
    last = _daily(sheet, report, figures, outage=outage, day_format=day_format)
    hourly = _intervals(sheet, report, figures, after=last, outage=outage, day_format=day_format)
    daily = (26, 20, 24 if outage else 0)
    for column, widths in enumerate(zip_longest(hourly, daily, fillvalue=0), 1):
        sheet.column_dimensions[get_column_letter(column)].width = max(widths)
    _print_setup(sheet)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
