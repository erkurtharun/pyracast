from __future__ import annotations

import re
from datetime import datetime
from io import BytesIO
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from django.utils import translation
from django.utils.translation import gettext as _
from openpyxl import Workbook
from openpyxl.cell.cell import Cell, MergedCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.worksheet import Worksheet

if TYPE_CHECKING:
    from pyracast.notifications.presentation import ForecastReport, ReportDay

_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


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
        messages.append(_("Forecast partial"))
    if has_outage and not outage_complete:
        messages.append(_("Outage partial"))
    return "; ".join(messages)


def _summary_status(day: ReportDay) -> str:
    messages = []
    if day.total is None:
        messages.append(_("Forecast partial") if day.coverage else _("Forecast unavailable"))
    if day.has_outage and day.outage_total is None:
        messages.append(
            _("Outage impact unknown")
            if day.outage_status == "unknown_impact"
            else _("Outage partial")
            if day.outage_coverage
            else _("Outage unavailable")
        )
    return "; ".join(messages)


def build_workbook(report: ForecastReport) -> bytes:
    zone = ZoneInfo(report.timezone)
    workbook = Workbook()
    sheet = cast(Worksheet, workbook.active)
    sheet.title = _("Forecast")
    sheet.sheet_view.showGridLines = False
    sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.print_options.horizontalCentered = True
    date_format = (
        "dd.mm.yyyy" if (translation.get_language() or "").startswith("tr") else "dd mmm yyyy"
    )
    datetime_format = date_format + " hh:mm"

    _literal(sheet.cell(1, 1), report.plant_name)
    sheet.cell(1, 1).font = Font(bold=True, size=16, color="17365D")
    _literal(sheet.cell(2, 1), _("Updated"))
    updated = sheet.cell(2, 2, _local(report.collected_at, zone) if report.collected_at else None)
    updated.number_format = datetime_format
    _literal(sheet.cell(2, 3), report.timezone)
    if report.collected_at is None:
        _literal(updated, _("Unavailable"))

    has_outage = any(day.has_outage for day in report.days)
    intervals = [interval for day in report.days for interval in day.intervals]
    offsets = {_offset(value, zone) for item in intervals for value in (item.start, item.end)}
    show_offsets = len(offsets) > 1
    show_status = any(
        not item.complete or (day.has_outage and not item.outage_complete)
        for day in report.days
        for item in day.intervals
    )
    show_summary_status = any(
        day.total is None or (day.has_outage and day.outage_total is None) for day in report.days
    )
    unit_label = {"unit": report.unit}
    forecast_label = (
        _("Normal operation (%(unit)s)") if has_outage else _("Forecast (%(unit)s)")
    ) % unit_label
    outage_label = _("With reported outage (%(unit)s)") % unit_label

    summary_headers = [_("Date"), forecast_label, _("Coverage")]
    if has_outage:
        summary_headers.extend([outage_label, _("Outage coverage")])
    if show_summary_status:
        summary_headers.append(_("Status"))
    for column, title in enumerate(summary_headers, 1):
        _literal(sheet.cell(4, column), title)
    for row, day in enumerate(report.days, 5):
        sheet.cell(row, 1, day.day).number_format = date_format
        sheet.cell(
            row, 2, day.total if day.total is not None else (day.partial if day.coverage else None)
        )
        sheet.cell(row, 2).number_format = "#,##0.000"
        sheet.cell(row, 3, day.coverage).number_format = "0%"
        status_column = 4
        if has_outage:
            if day.has_outage:
                sheet.cell(
                    row,
                    4,
                    day.outage_total
                    if day.outage_total is not None
                    else (day.outage_partial if day.outage_coverage else None),
                ).number_format = "#,##0.000"
                sheet.cell(row, 5, day.outage_coverage).number_format = "0%"
            status_column = 6
        if show_summary_status:
            status = _summary_status(day)
            _literal(sheet.cell(row, status_column), status)
            sheet.cell(row, status_column).alignment = Alignment(wrap_text=True, vertical="center")
            if len(status) > 22:
                sheet.row_dimensions[row].height = 30

    header_row = 6 + len(report.days)
    headers = [_("Date"), _("From"), _("To")]
    if show_offsets:
        headers.extend([_("From UTC offset"), _("To UTC offset")])
    headers.append(forecast_label)
    if has_outage:
        headers.append(outage_label)
    if show_status:
        headers.append(_("Status"))
    for column, title in enumerate(headers, 1):
        _literal(sheet.cell(header_row, column), title)
    row = header_row
    for day in report.days:
        for item in day.intervals:
            row += 1
            sheet.cell(row, 1, day.day).number_format = date_format
            start = _local(item.start, zone)
            end = _local(item.end, zone)
            sheet.cell(row, 2, start).number_format = "hh:mm"
            sheet.cell(row, 3, end).number_format = (
                "dd mmm hh:mm" if end.date() != start.date() else "hh:mm"
            )
            column = 4
            if show_offsets:
                _literal(sheet.cell(row, column), _offset(item.start, zone))
                _literal(sheet.cell(row, column + 1), _offset(item.end, zone))
                column += 2
            sheet.cell(row, column, item.energy).number_format = "#,##0.000"
            column += 1
            if has_outage:
                sheet.cell(
                    row, column, item.outage_energy if day.has_outage else None
                ).number_format = "#,##0.000"
                column += 1
            if show_status:
                status = _status(item.complete, item.outage_complete, day.has_outage)
                _literal(sheet.cell(row, column), status)
                sheet.cell(row, column).alignment = Alignment(wrap_text=True, vertical="center")
                if len(status) > 22:
                    sheet.row_dimensions[row].height = 30

    for heading_row in (4, header_row):
        sheet.row_dimensions[heading_row].height = 32
        for cell in sheet[heading_row]:
            cell.font = Font(bold=True, color="FFFFFF", size=10)
            cell.fill = PatternFill("solid", fgColor="17365D")
            cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.freeze_panes = f"A{header_row + 1}"
    sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(len(headers))}{max(row, header_row)}"
    row += 1
    for warning in report.warnings:
        row += 1
        _literal(sheet.cell(row, 1), warning)
    for column in range(1, max(len(headers), len(summary_headers)) + 1):
        sheet.column_dimensions[get_column_letter(column)].width = 16 if column == 1 else 22
    sheet.print_area = f"A1:{get_column_letter(max(len(headers), len(summary_headers)))}{row}"
    sheet.print_title_rows = f"{header_row}:{header_row}"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
