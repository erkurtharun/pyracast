from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from io import BytesIO
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

type Value = str | float | date | datetime | None

_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_INK = "09383A"
_WHITE = "FFFFFF"
_TINT = "DDEBEA"
_NOTE = "F1F5F9"
_CENTRED = Alignment(horizontal="center", vertical="center", wrap_text=True)
_TOTAL = "#,##0.00"
_INTERVAL = "#,##0.000"
_LINE_POINTS = 15
_TITLE_CHARACTERS = 24
_TITLE_LINE_POINTS = 19


def _clean(value: str) -> str:
    return _ILLEGAL_XML.sub("", value)


def _quoted(value: str) -> str:
    return '"' + _clean(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lines(text: str, characters: int) -> int:
    return sum(len(wrap(line, characters)) or 1 for line in text.split("\n"))


def _row(
    sheet: Worksheet,
    row: int,
    widths: Sequence[int],
    fill: str,
    cells: Sequence[tuple[Value, int, str]],
) -> Cell:
    lines, column = 1, 1
    for value, span, shown in cells:
        cell = cast(Cell, sheet.cell(row, column))
        if isinstance(value, str):
            cell.value = _clean(value)
            cell.data_type = "s"
            lines = max(lines, _lines(value, sum(widths[column - 1 : column - 1 + span]) - 2))
        else:
            cell.value = value
            cell.number_format = shown
            lines = max(lines, shown.count("\n") + 1)
        if span > 1:
            sheet.merge_cells(
                start_row=row, start_column=column, end_row=row, end_column=column + span - 1
            )
        column += span
    dark = fill == _INK
    for column in range(1, len(widths) + 1):
        styled = sheet.cell(row, column)
        styled.font = Font(size=11 if dark else 12, bold=dark, color=_WHITE if dark else _INK)
        styled.fill = PatternFill("solid", fgColor=fill)
        styled.alignment = _CENTRED
    sheet.row_dimensions[row].height = min(409, lines * _LINE_POINTS + 10)
    return cast(Cell, sheet.cell(row, 1))


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


def _top(sheet: Worksheet, report: ForecastReport, widths: Sequence[int], day_format: str) -> None:
    full = len(widths)
    title = _row(sheet, 1, widths, _WHITE, [(report.plant_name, full, "")])
    title.font = Font(size=14, bold=True, color=_INK)
    sheet.row_dimensions[1].height = min(
        409, _lines(cast(str, title.value), _TITLE_CHARACTERS) * _TITLE_LINE_POINTS + 4
    )
    subtitle = (
        _("Generation forecast · Pyracast")
        if report.electrical
        else _("Solar irradiance forecast (GHI) · Pyracast")
    )
    _row(sheet, 2, widths, _WHITE, [(subtitle, full, "")])
    zone = ZoneInfo(report.timezone)
    collected = report.collected_at.astimezone(zone) if report.collected_at else None
    reference = collected or datetime.combine(report.days[-1].day, datetime.min.time(), zone)
    data_time: tuple[Value, int, str] = (
        (
            collected.replace(tzinfo=None),
            full,
            _quoted(f"{_('Forecast data')}: ") + f"{day_format} hh:mm",
        )
        if collected
        else (_("Forecast data"), full, "")
    )
    _row(sheet, 4, widths, _WHITE, [data_time])
    zone_text = f"{_('Time zone')}: {report.timezone} ({utc_offset(reference)})"
    _row(sheet, 5, widths, _WHITE, [(zone_text, full, "")])
    _row(sheet, 6, widths, _WHITE, [(f"{_('Unit')}: {report.unit}", full, "")])


def _heading(sheet: Worksheet, row: int, widths: Sequence[int], title: str) -> None:
    _row(sheet, row, widths, _WHITE, [(title, len(widths), "")]).font = Font(
        size=12, bold=True, color=_INK
    )


def _daily(
    sheet: Worksheet,
    report: ForecastReport,
    figures: list[Figure],
    widths: Sequence[int],
    *,
    outage: bool,
    day_format: str,
) -> int:
    _heading(sheet, 8, widths, _("Daily totals"))
    titles = [(_value_label(report), 1, "")]
    if outage:
        titles.append((_("Without outage (MWh)"), 1, ""))
    _row(sheet, 9, widths, _INK, [(_("Date"), 2, ""), *titles])
    row = 9
    for position, (day, shown) in enumerate(zip(report.days, figures, strict=True)):
        row += 1
        cells: list[tuple[Value, int, str]] = [(day.day, 2, day_format), (shown.value, 1, _TOTAL)]
        if outage:
            without = shown.without_outage if shown.outage == "included" else shown.value
            cells.append((without, 1, _TOTAL))
        _row(sheet, row, widths, _TINT if position % 2 else _WHITE, cells)
    for note in _notes(report, figures, outage):
        row += 1
        _row(sheet, row, widths, _NOTE, [(note, len(widths), "")])
    return row


def _time(moment: datetime, zone: ZoneInfo, *, offset: bool) -> tuple[Value, int, str]:
    local = moment.astimezone(zone)
    shown = "hh:mm" + (_quoted("\n" + utc_offset(local)) if offset else "")
    return local.replace(tzinfo=None), 1, shown


def _days(
    sheet: Worksheet,
    report: ForecastReport,
    figures: list[Figure],
    widths: Sequence[int],
    *,
    after: int,
    outage: bool,
    day_format: str,
) -> int:
    zone = ZoneInfo(report.timezone)
    titles: list[tuple[Value, int, str]] = [
        (_("From"), 1, ""),
        (_("To"), 1, ""),
        (_value_label(report), 1, ""),
    ]
    if outage:
        titles.append((_("Without outage (MWh)"), 1, ""))
    row = after + 2
    _heading(sheet, row, widths, _("Hourly values") if report.electrical else _("Interval values"))
    for position, (day, shown) in enumerate(zip(report.days, figures, strict=True)):
        start, end = local_day_bounds(day.day, report.timezone)
        offset = end - start != timedelta(days=1)
        fill = _TINT if position % 2 else _WHITE
        row += 2
        _row(sheet, row, widths, _INK, [(day.day, len(widths), day_format)])
        row += 1
        _row(sheet, row, widths, _INK, titles)
        for item in day.intervals:
            row += 1
            cells = [
                _time(item.start, zone, offset=offset),
                _time(item.end, zone, offset=offset),
                (interval_value(item, shown), 1, _INTERVAL),
            ]
            if outage:
                cells.append((item.energy if item.complete else None, 1, _INTERVAL))
            _row(sheet, row, widths, fill, cells)
    return row


def build_workbook(report: ForecastReport) -> bytes:
    figures = [figure(day) for day in report.days]
    outage = any(shown.outage == "included" for shown in figures)
    widths = (9, 9, 12, 12) if outage else (9, 9, 24)
    day_format = pgettext("Excel date format", "dd mmm yyyy")
    workbook = Workbook()
    workbook.properties.creator = "Pyracast"
    workbook.properties.lastModifiedBy = "Pyracast"
    workbook.properties.title = _clean(f"{report.plant_name} {day_text(report.days[-1].day)}")
    sheet = cast(Worksheet, workbook.active)
    sheet.title = (
        pgettext("workbook sheet", "Forecast")
        if report.electrical
        else pgettext("workbook sheet", "Irradiance")
    )
    for column, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(column)].width = width
    _top(sheet, report, widths, day_format)
    last = _daily(sheet, report, figures, widths, outage=outage, day_format=day_format)
    last = _days(sheet, report, figures, widths, after=last, outage=outage, day_format=day_format)
    sheet.sheet_view.showGridLines = False
    sheet.sheet_view.showRowColHeaders = False
    sheet.sheet_view.zoomScale = 100
    sheet.sheet_view.zoomScaleNormal = 100
    sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.print_options.horizontalCentered = True
    sheet.print_area = f"A1:{get_column_letter(len(widths))}{last}"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
