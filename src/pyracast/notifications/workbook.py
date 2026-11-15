from __future__ import annotations

import re
from datetime import datetime
from io import BytesIO
from textwrap import wrap
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from django.utils import translation
from django.utils.translation import gettext as _
from openpyxl import Workbook
from openpyxl.cell.cell import Cell, MergedCell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.worksheet import Worksheet

from pyracast.notifications.presentation import source_label

if TYPE_CHECKING:
    from pyracast.notifications.presentation import ForecastReport, ReportDay

_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_INK = "17365D"


def _literal(cell: Cell | MergedCell, value: str) -> None:
    target = cast(Cell, cell)
    target.value = _ILLEGAL_XML.sub("", value)
    target.data_type = "s"


def _local(value: datetime, zone: ZoneInfo) -> datetime:
    return value.astimezone(zone).replace(tzinfo=None)


def _offset(value: datetime, zone: ZoneInfo) -> str:
    offset = value.astimezone(zone).strftime("%z")
    return f"UTC{offset[:3]}:{offset[3:]}"


def _status(forecast_complete: bool, outage_complete: bool, has_outage: bool) -> str:
    messages: list[str] = []
    if not forecast_complete:
        messages.append(_("Partial estimate"))
    if has_outage and not outage_complete:
        messages.append(_("Outage partial"))
    return "; ".join(messages)


def _summary_status(day: ReportDay) -> str:
    messages = []
    if day.total is None:
        messages.append(_("Partial estimate") if day.coverage else _("Estimate unavailable"))
    if day.has_outage and day.outage_total is None:
        messages.append(
            _("Outage impact unknown")
            if day.outage_status == "unknown_impact"
            else _("Outage partial")
            if day.outage_coverage
            else _("Outage unavailable")
        )
    return "; ".join(messages)


def _style_row(
    sheet: Worksheet, row: int, columns: int, *, fill: str = "FFFFFF", heading: bool = False
) -> None:
    for column in range(1, columns + 1):
        cell = sheet.cell(row, column)
        cell.font = Font(
            name="Calibri",
            size=11 if heading else 12,
            bold=heading,
            color="FFFFFF" if heading else _INK,
        )
        cell.fill = PatternFill("solid", fgColor=fill)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.row_dimensions[row].height = 25


def _text_height(value: str, width: int, *, line_height: int = 15) -> int:
    lines = sum(max(1, len(wrap(line, width=width))) for line in value.split("\n"))
    return lines * line_height + 10


def _note(sheet: Worksheet, row: int, columns: int, text: str, *, fill: str = "F1F5F9") -> None:
    _style_row(sheet, row, columns, fill=fill)
    sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=columns)
    _literal(sheet.cell(row, 1), text)
    width = int(
        sum(sheet.column_dimensions[get_column_letter(c)].width for c in range(1, columns + 1))
    )
    sheet.row_dimensions[row].height = _text_height(text, width - 3)


def _headers(sheet: Worksheet, row: int, titles: tuple[str, ...], *, summary: bool) -> None:
    columns = len(titles) + int(summary)
    _style_row(sheet, row, columns, fill=_INK, heading=True)
    if summary:
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
    heights = []
    for column, title in enumerate(titles, 1):
        target = column + int(summary and column > 1)
        _literal(sheet.cell(row, target), title)
        width = sheet.column_dimensions[get_column_letter(target)].width
        if summary and column == 1:
            width += sheet.column_dimensions["B"].width
        heights.append(_text_height(title, max(1, int(width) - 1)))
    sheet.row_dimensions[row].height = max(heights)


def _energy(sheet: Worksheet, row: int, column: int, value: float | None) -> None:
    sheet.cell(row, column, value).number_format = (
        "0.000E+00" if value is not None and abs(value) >= 1_000_000 else "#,##0.000"
    )


def _summary_notes(day: ReportDay, *, show_source: bool) -> str:
    notes = []
    if day.coverage is not None and day.coverage < 1:
        notes.append(f"{_('Coverage')}: {day.coverage:.0%}")
    if day.has_outage and day.outage_coverage is not None and day.outage_coverage < 1:
        notes.append(f"{_('Outage coverage')}: {day.outage_coverage:.0%}")
    if status := _summary_status(day):
        notes.append(status)
    if show_source:
        notes.append(f"{_('Source')}: {source_label(day.source_kind)}")
    return " · ".join(notes)


def _quoted(value: str) -> str:
    return '"' + _ILLEGAL_XML.sub("", value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _time_format(
    value: datetime, zone: ZoneInfo, *, date_format: str, include_date: bool, show_offsets: bool
) -> str:
    number_format = "hh:mm"
    if include_date:
        number_format += _quoted("\n") + ("dd.mm" if date_format == "dd.mm.yyyy" else "dd mmm")
    if show_offsets:
        number_format += _quoted("\n" + _offset(value, zone))
    return number_format


def _day_intervals(
    sheet: Worksheet,
    row: int,
    day: ReportDay,
    *,
    zone: ZoneInfo,
    columns: int,
    date_format: str,
    labels: tuple[str, ...],
    show_source: bool,
    show_offsets: bool,
    fill: str,
) -> int:
    _style_row(sheet, row, columns, fill=_INK, heading=True)
    sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=columns)
    sheet.cell(row, 1, day.day).number_format = date_format
    sheet.cell(row, 1).border = Border(top=Side(style="medium", color="839DBB"))
    row += 1
    _headers(sheet, row, (_("From"), _("To"), *labels), summary=False)
    previous_source = None
    for item in day.intervals:
        if show_source and item.source_kind != previous_source:
            row += 1
            _note(sheet, row, columns, f"{_('Source')}: {source_label(item.source_kind)}")
            previous_source = item.source_kind
        row += 1
        _style_row(sheet, row, columns, fill=fill)
        start, end = _local(item.start, zone), _local(item.end, zone)
        for column, instant, local in ((1, item.start, start), (2, item.end, end)):
            sheet.cell(row, column, local).number_format = _time_format(
                instant,
                zone,
                date_format=date_format,
                include_date=local.date() != day.day,
                show_offsets=show_offsets,
            )
        lines = 1 + int(show_offsets) + int(start.date() != day.day or end.date() != day.day)
        sheet.row_dimensions[row].height = 15 * lines + 10
        _energy(sheet, row, 3, item.energy)
        if columns == 4:
            _energy(sheet, row, 4, item.outage_energy if day.has_outage else None)
        if status := _status(item.complete, item.outage_complete, day.has_outage):
            row += 1
            _note(sheet, row, columns, status, fill=fill)
    return row


def build_workbook(report: ForecastReport) -> bytes:
    zone = ZoneInfo(report.timezone)
    workbook = Workbook()
    sheet = cast(Worksheet, workbook.active)
    sheet.title = _("Forecast")
    sheet.sheet_view.showGridLines = False
    sheet.sheet_view.showRowColHeaders = False
    sheet.sheet_view.view = "normal"
    sheet.sheet_view.zoomScale = 100
    sheet.sheet_view.zoomScaleNormal = 100
    sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.print_options.horizontalCentered = True
    date_format = (
        "dd.mm.yyyy" if (translation.get_language() or "").startswith("tr") else "dd mmm yyyy"
    )
    has_outage = any(day.has_outage for day in report.days)
    columns = 4 if has_outage else 3
    widths = (9, 9, 12, 12) if has_outage else (9, 9, 24)
    for column, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(column)].width = width
    _note(sheet, 1, columns, report.plant_name, fill="EAF1F8")
    sheet.cell(1, 1).font = Font(name="Calibri", size=14, bold=True, color=_INK)
    title_width = (
        int(
            sum(sheet.column_dimensions[get_column_letter(c)].width for c in range(1, columns + 1))
            * 11
            / 14
        )
        - 3
    )
    title_measure = "".join(
        character * (2 if character in "MW@" else 1) for character in report.plant_name
    )
    sheet.row_dimensions[1].height = min(
        409, _text_height(title_measure, title_width, line_height=19)
    )
    _note(sheet, 2, columns, "")
    if report.collected_at is None:
        _literal(sheet.cell(2, 1), f"{_('Updated')}: {_('Unavailable')}")
    else:
        updated = sheet.cell(2, 1, _local(report.collected_at, zone))
        updated.number_format = _quoted(_("Updated") + ": ") + date_format + " hh:mm"
    _note(sheet, 3, columns, report.timezone)

    show_source = any(
        day.source_kind != "forecast"
        or any(item.source_kind != "forecast" for item in day.intervals)
        for day in report.days
    )
    offsets = {
        _offset(instant, zone)
        for day in report.days
        for item in day.intervals
        for instant in (item.start, item.end)
    }
    unit_label = {"unit": report.unit}
    forecast_label = (
        _("Normal operation (%(unit)s)")
        if has_outage
        else _("Estimate (%(unit)s)")
        if show_source
        else _("Forecast (%(unit)s)")
    ) % unit_label
    labels: tuple[str, ...] = (forecast_label,)
    if has_outage:
        labels += (_("With reported outage (%(unit)s)") % unit_label,)
    _headers(sheet, 4, (_("Date"), *labels), summary=True)
    row = 4
    for index, day in enumerate(report.days):
        row += 1
        _style_row(sheet, row, columns, fill="EAF1F8" if index % 2 == 0 else "FFFFFF")
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        sheet.cell(row, 1, day.day).number_format = date_format
        _energy(
            sheet,
            row,
            3,
            day.total if day.total is not None else day.partial if day.coverage else None,
        )
        if has_outage:
            value = (
                day.outage_total
                if day.outage_total is not None
                else day.outage_partial
                if day.outage_coverage
                else None
            )
            _energy(sheet, row, 4, value if day.has_outage else None)
        if notes := _summary_notes(day, show_source=show_source):
            row += 1
            _note(sheet, row, columns, notes)
    for index, day in enumerate(report.days):
        row += 2
        row = _day_intervals(
            sheet,
            row,
            day,
            zone=zone,
            columns=columns,
            date_format=date_format,
            labels=labels,
            show_source=show_source,
            show_offsets=len(offsets) > 1,
            fill="EAF1F8" if index % 2 == 0 else "FFFFFF",
        )
    if report.warnings:
        row += 1
    for warning in report.warnings:
        row += 1
        _note(sheet, row, columns, warning, fill="FFF4D6")
    sheet.print_area = f"A1:{get_column_letter(columns)}{row}"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
