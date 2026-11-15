from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from django.utils import translation
from openpyxl import load_workbook
from openpyxl.cell.cell import Cell
from openpyxl.worksheet.worksheet import Worksheet

from pyracast.notifications.presentation import ForecastReport, ReportDay, ReportInterval
from pyracast.notifications.workbook import build_workbook


def _report(*, outage: bool = False, incomplete: bool = False, dst: bool = False) -> ForecastReport:
    zone = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 10, 25, 0 if dst else 10, tzinfo=UTC).astimezone(zone)
    end = datetime(2026, 10, 25, 1 if dst else 11, tzinfo=UTC).astimezone(zone)
    interval = ReportInterval(
        start=start,
        end=end,
        energy=0.0 if not incomplete else None,
        outage_energy=0.0 if outage else None,
        complete=not incomplete,
        outage_complete=not incomplete,
    )
    day = ReportDay(
        day=date(2026, 10, 25),
        total=None if incomplete else 0.0,
        partial=0.0,
        coverage=0.5 if incomplete else 1.0,
        outage_total=0.0 if outage else None,
        outage_partial=0.0,
        outage_coverage=1.0 if outage else None,
        has_outage=outage,
        intervals=(interval,),
    )
    return ForecastReport(
        plant_name="=unsafe\x01",
        timezone="Europe/Berlin",
        collected_at=start,
        prepared_at=start,
        days=(day,),
        unit="MWh",
        electrical=True,
        warnings=(),
        url="https://example.com",
    )


def _sheet(report: ForecastReport) -> Worksheet:
    with translation.override("en"):
        workbook = load_workbook(BytesIO(build_workbook(report)))
    assert len(workbook.sheetnames) == 1
    return cast(Worksheet, workbook.active)


def _date_rows(sheet: Worksheet, day: date) -> list[int]:
    return [
        cell.row for cell in sheet["A"] if cell.value == datetime.combine(day, datetime.min.time())
    ]


def _rows_with(sheet: Worksheet, text: str) -> list[int]:
    return [
        cell.row
        for row in sheet
        for cell in row
        if isinstance(cell, Cell) and text in str(cell.value or "")
    ]


def test_workbook_contains_typed_values_and_safe_text() -> None:
    sheet = _sheet(_report())
    assert sheet["A1"].value == "=unsafe"
    assert sheet["A1"].data_type == "s"
    assert "A1:C1" in sheet.merged_cells
    assert "A2:C2" in sheet.merged_cells
    assert isinstance(sheet["A2"].value, datetime)
    assert sheet["A3"].value == "Europe/Berlin"
    assert sheet["A3"].data_type == "s"
    assert "A3:C3" in sheet.merged_cells
    assert "\n" not in sheet["A2"].number_format
    summary, banner = _date_rows(sheet, date(2026, 10, 25))
    assert f"A{summary}:B{summary}" in sheet.merged_cells
    assert f"A{banner}:C{banner}" in sheet.merged_cells
    assert sheet.cell(summary, 3).value == 0
    assert sheet.cell(summary, 3).data_type == "n"
    header = _rows_with(sheet, "From")[0]
    assert [sheet.cell(header, column).value for column in (1, 2, 3)] == [
        "From",
        "To",
        "Forecast (MWh)",
    ]
    interval = header + 1
    assert all(isinstance(sheet.cell(interval, col).value, datetime) for col in (1, 2))
    assert sheet.cell(interval, 3).value == 0
    assert sheet.max_column == 3
    assert sheet.freeze_panes is None
    assert sheet.auto_filter.ref is None
    assert not sheet.tables
    assert sheet.sheet_view.showGridLines is False
    assert sheet.sheet_view.showRowColHeaders is False
    assert sheet.sheet_view.view == "normal"
    assert sheet.sheet_view.zoomScale == sheet.sheet_view.zoomScaleNormal == 100
    assert sheet.column_dimensions["C"].width == 24
    assert sheet.cell(interval, 3).font.sz == 12
    assert sheet.page_setup.fitToWidth == 1
    for row in (header, interval):
        for column in range(1, 4):
            assert sheet.cell(row, column).alignment.horizontal == "center"


def test_workbook_preserves_gaps_and_conditional_outage() -> None:
    sheet = _sheet(_report(outage=True, incomplete=True))
    summary, _ = _date_rows(sheet, date(2026, 10, 25))
    assert sheet.max_column == 4
    assert sheet.cell(summary, 3).value == 0
    assert sheet.cell(summary, 4).value == 0
    assert "Partial estimate" in str(sheet.cell(summary + 1, 1).value)
    header = _rows_with(sheet, "From")[0]
    assert sheet.cell(header, 3).value == "Normal operation (MWh)"
    assert sheet.cell(header, 4).value == "With reported outage (MWh)"
    assert sheet.cell(header + 1, 3).value is None
    assert sheet.cell(header + 1, 4).value == 0
    assert "Partial estimate" in str(sheet.cell(header + 2, 1).value)
    assert "Outage partial" in str(sheet.cell(header + 2, 1).value)
    assert f"A{header + 2}:D{header + 2}" in sheet.merged_cells


def test_workbook_disambiguates_clock_change() -> None:
    sheet = _sheet(_report(dst=True))
    row = _rows_with(sheet, "From")[0] + 1
    assert sheet.cell(row, 1).value == sheet.cell(row, 2).value
    assert "UTC+02:00" in sheet.cell(row, 1).number_format
    assert "UTC+01:00" in sheet.cell(row, 2).number_format
    assert "\n" in sheet.cell(row, 1).number_format


def test_workbook_distinguishes_outage_gap_and_midnight() -> None:
    report = _report(outage=True)
    day = report.days[0]
    start = datetime(2026, 10, 25, 23, tzinfo=ZoneInfo("Europe/Berlin"))
    interval = replace(
        day.intervals[0],
        start=start,
        end=start + timedelta(hours=1),
        outage_energy=None,
        outage_complete=False,
    )
    day = replace(day, outage_total=None, outage_coverage=0.5, intervals=(interval,))
    sheet = _sheet(replace(report, days=(day,)))
    row = _rows_with(sheet, "From")[0] + 1
    assert sheet.cell(row, 2).value == datetime(2026, 10, 26)
    assert "dd" in sheet.cell(row, 2).number_format
    assert "Outage partial" in str(sheet.cell(row + 1, 1).value)


@pytest.mark.parametrize("unknown", [True, False])
def test_workbook_distinguishes_no_outage_from_unavailable(unknown: bool) -> None:
    report = _report(outage=True, incomplete=True)
    unavailable = replace(
        report.days[0],
        total=None,
        coverage=0,
        outage_total=None,
        outage_coverage=0,
        outage_status="unknown_impact" if unknown else "estimated",
    )
    unaffected = replace(_report().days[0], day=date(2026, 10, 26))
    sheet = _sheet(replace(report, collected_at=None, days=(unavailable, unaffected)))
    assert "Unavailable" in str(sheet["A2"].value)
    assert sheet["A3"].value == report.timezone
    row = _date_rows(sheet, unavailable.day)[0]
    assert sheet.cell(row, 3).value is None
    assert sheet.cell(row, 4).value is None
    note = str(sheet.cell(row + 1, 1).value)
    assert "Estimate unavailable" in note
    assert ("Outage impact unknown" if unknown else "Outage unavailable") in note
    row = _date_rows(sheet, unaffected.day)[0]
    assert sheet.cell(row, 3).value == 0
    assert sheet.cell(row, 4).value is None


@pytest.mark.parametrize("language", ["en", "tr"])
def test_workbook_keeps_days_and_source_transitions(language: str) -> None:
    report = _report()
    first = report.days[0]
    estimated = replace(first.intervals[0], source_kind="estimated_actuals")
    first = replace(first, source_kind="mixed", intervals=(estimated, first.intervals[0]))
    second = replace(
        first,
        day=first.day + timedelta(days=1),
        source_kind="forecast",
        intervals=(
            replace(
                first.intervals[0],
                start=first.intervals[0].start + timedelta(days=1),
                end=first.intervals[0].end + timedelta(days=1),
                source_kind="forecast",
            ),
        ),
    )
    with translation.override(language):
        sheet = cast(
            Worksheet,
            load_workbook(BytesIO(build_workbook(replace(report, days=(first, second))))).active,
        )
    for day in (first.day, second.day):
        summary, banner = _date_rows(sheet, day)
        assert summary < banner
        assert sheet.cell(summary, 3).value == 0
        assert f"A{banner}:C{banner}" in sheet.merged_cells
    forecast_summary = _date_rows(sheet, second.day)[0]
    assert ("Forecast" if language == "en" else "Tahmin") in str(
        sheet.cell(forecast_summary + 1, 1).value
    )
    source_rows = [
        row
        for row in _rows_with(sheet, "Source:" if language == "en" else "Kaynak:")
        if row > _date_rows(sheet, first.day)[1]
    ]
    assert len(source_rows) == 3
    assert all(f"A{row}:C{row}" in sheet.merged_cells for row in source_rows)
    assert sheet.cell(source_rows[0], 1).value != sheet.cell(source_rows[1], 1).value
    assert sheet.cell(source_rows[1], 1).value == sheet.cell(source_rows[2], 1).value


def test_workbook_preserves_warning_long_title_and_mixed_source() -> None:
    report = _report(outage=True, incomplete=True, dst=True)
    interval = replace(
        report.days[0].intervals[0], energy=1.25, outage_energy=None, source_kind="mixed"
    )
    day = replace(
        report.days[0],
        partial=1.25,
        outage_total=None,
        outage_coverage=0,
        outage_status="unknown_impact",
        source_kind="mixed",
        intervals=(interval,),
    )
    title = "A long plant title " * 10
    sheet = _sheet(
        replace(report, plant_name=title, warnings=("Warning: check data",), days=(day,))
    )
    assert sheet.max_column == 4
    assert sheet.auto_filter.ref is None
    assert not sheet.tables
    assert sheet.column_dimensions["C"].width >= 12
    assert sheet.column_dimensions["D"].width >= 12
    assert all(
        cell.alignment.horizontal == "center"
        for row in sheet
        for cell in row
        if cell.value is not None
    )
    assert sheet["A1"].value == title
    assert sheet["A1"].alignment.wrap_text
    assert _rows_with(sheet, "Warning: check data")
    summary = _date_rows(sheet, day.day)[0]
    assert sheet.cell(summary, 3).value == 1.25
    assert sheet.cell(summary, 4).value is None
    assert "Outage impact unknown" in str(sheet.cell(summary + 1, 1).value)
    assert _rows_with(sheet, "Estimated + forecast")
    row = _rows_with(sheet, "From")[0] + 1
    while not isinstance(sheet.cell(row, 1).value, datetime):
        row += 1
    assert sheet.cell(row, 3).value == 1.25
    assert sheet.cell(row, 4).value is None


def test_workbook_keeps_large_energy_numeric_in_narrow_columns() -> None:
    report = _report(outage=True)
    interval = replace(report.days[0].intervals[0], energy=1_234_567.0, outage_energy=-1_234_567.0)
    day = replace(
        report.days[0], total=1_234_567.0, outage_total=-1_234_567.0, intervals=(interval,)
    )
    sheet = _sheet(replace(report, days=(day,)))
    summary = _date_rows(sheet, day.day)[0]
    interval_row = _rows_with(sheet, "From")[0] + 1
    for row in (summary, interval_row):
        for column, value in ((3, 1_234_567.0), (4, -1_234_567.0)):
            cell = sheet.cell(row, column)
            assert cell.value == value
            assert cell.data_type == "n"
            assert cell.number_format == "0.000E+00"
    assert max(sheet.column_dimensions[column].width for column in "ABCD") <= 12


def test_workbook_allocates_height_for_long_wide_title() -> None:
    report = _report()
    narrow = _sheet(replace(report, plant_name="i" * 100))
    long = _sheet(replace(report, plant_name="W" * 100))
    assert long["A1"].value == "W" * 100
    assert long["A1"].alignment.wrap_text
    assert long.row_dimensions[1].height > narrow.row_dimensions[1].height
