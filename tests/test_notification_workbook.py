from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from django.utils import translation
from openpyxl import load_workbook
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


def test_workbook_contains_typed_values_and_safe_text() -> None:
    sheet = _sheet(_report())
    assert sheet["A1"].value == "=unsafe"
    assert sheet["A1"].data_type == "s"
    assert isinstance(sheet["B2"].value, datetime)
    assert isinstance(sheet["A5"].value, datetime)
    assert sheet["D8"].value == 0
    assert sheet["D8"].data_type == "n"
    assert sheet["D7"].value == "Forecast (MWh)"
    assert sheet.max_column == 4
    assert sheet.freeze_panes == "A8"
    assert sheet.auto_filter.ref == "A7:D8"
    assert sheet.page_setup.fitToWidth == 1
    assert sheet.sheet_view.showGridLines is False
    assert sheet.column_dimensions["A"].width == 16
    assert sheet["B8"].number_format == "hh:mm"
    assert sheet["D7"].alignment.wrap_text
    assert sheet.row_dimensions[7].height == 32
    assert "$A$1:$D$9" in str(sheet.print_area)


def test_workbook_preserves_gaps_and_conditional_outage() -> None:
    sheet = _sheet(_report(outage=True, incomplete=True))
    assert sheet["B5"].value == 0
    assert sheet["F5"].value == "Partial estimate"
    assert sheet["D7"].value == "Normal operation (MWh)"
    assert sheet["E7"].value == "With reported outage (MWh)"
    assert sheet["D8"].value is None
    assert sheet["E8"].value == 0
    assert sheet["F8"].value == "Partial estimate; Outage partial"


def test_workbook_disambiguates_clock_change() -> None:
    sheet = _sheet(_report(dst=True))
    assert sheet["B8"].value == sheet["C8"].value
    assert sheet["D8"].value == "UTC+02:00"
    assert sheet["E8"].value == "UTC+01:00"


@pytest.mark.parametrize("language", ["en", "tr"])
def test_workbook_separates_days_without_interrupting_filtered_data(language: str) -> None:
    report = _report(incomplete=True)
    first = report.days[0]
    second = replace(
        first,
        day=first.day + timedelta(days=1),
        intervals=tuple(
            replace(item, start=item.start + timedelta(days=1), end=item.end + timedelta(days=1))
            for item in first.intervals
        ),
    )
    first = replace(first, intervals=first.intervals * 2)
    with translation.override(language):
        workbook = load_workbook(BytesIO(build_workbook(replace(report, days=(first, second)))))
    sheet = cast(Worksheet, workbook.active)
    assert sheet.auto_filter.ref == "A8:E11"
    for column in range(1, 6):
        assert sheet.cell(9, column).fill.patternType == "solid"
        assert sheet.cell(9, column).fill.fgColor.rgb == sheet.cell(10, column).fill.fgColor.rgb
        assert sheet.cell(9, column).fill.fgColor.rgb != sheet.cell(11, column).fill.fgColor.rgb
        assert sheet.cell(9, column).border.top.style == "medium"
        assert sheet.cell(10, column).border.top.style is None
        assert sheet.cell(11, column).border.top.style == "medium"
    assert sheet["A9"].font.bold
    assert sheet["A11"].font.bold
    assert sheet["A11"].value == datetime(2026, 10, 26)
    assert sheet["D11"].value is None


def test_workbook_aligns_headers_with_typed_data_and_status() -> None:
    sheet = _sheet(_report(outage=True, incomplete=True, dst=True))
    for column in range(1, 7):
        expected = "left" if column in (1, 6) else "right"
        assert sheet.cell(4, column).alignment.horizontal == expected
        assert sheet.cell(5, column).alignment.horizontal == expected
    for column in range(1, 9):
        expected = "left" if column in (1, 8) else "center" if column <= 5 else "right"
        assert sheet.cell(7, column).alignment.horizontal == expected
        assert sheet.cell(8, column).alignment.horizontal == expected
    assert sheet["H8"].alignment.wrap_text


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
    assert sheet["F5"].value == "Outage partial"
    assert sheet["F8"].value == "Outage partial"
    assert sheet["C8"].number_format == "dd mmm hh:mm"


@pytest.mark.parametrize("unknown", [True, False])
def test_workbook_does_not_confuse_no_outage_with_unavailable_outage(unknown: bool) -> None:
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
    assert sheet["B2"].value == "Unavailable"
    assert sheet["B5"].value is None
    assert sheet["D5"].value is None
    assert sheet["F5"].value == (
        "Estimate unavailable; Outage impact unknown"
        if unknown
        else "Estimate unavailable; Outage unavailable"
    )
    assert sheet["B6"].value == 0
    assert sheet["D6"].value is None
    assert sheet["F6"].value is None
    assert sheet.row_dimensions[5].height == 30


@pytest.mark.parametrize("language", ["en", "tr"])
def test_actuals_workbook_keeps_daily_totals_and_sources_aligned_across_days(language: str) -> None:
    report = _report()
    first = replace(
        report.days[0],
        source_kind="estimated_actuals",
        intervals=tuple(
            replace(row, source_kind="estimated_actuals") for row in report.days[0].intervals
        ),
    )
    second = replace(
        report.days[0],
        day=date(2026, 10, 26),
        intervals=tuple(
            replace(row, start=row.start + timedelta(days=1), end=row.end + timedelta(days=1))
            for row in report.days[0].intervals
        ),
    )
    with translation.override(language):
        workbook = load_workbook(BytesIO(build_workbook(replace(report, days=(first, second)))))
    sheet = cast(Worksheet, workbook.active)
    date_label, estimate, coverage, source, beginning, end = (
        ("Date", "Estimate (MWh)", "Coverage", "Source", "From", "To")
        if language == "en"
        else ("Tarih", "Tahmin (MWh)", "Kapsam", "Kaynak", "Başlang\u0131ç", "Bitiş")
    )
    assert [sheet.cell(4, column).value for column in range(1, 5)] == [
        date_label,
        estimate,
        coverage,
        source,
    ]
    assert [sheet.cell(8, column).value for column in range(1, 6)] == [
        date_label,
        beginning,
        end,
        estimate,
        source,
    ]
    assert sheet["B5"].value == sheet["B6"].value == 0
    assert (
        sheet["D5"].value
        == sheet["E9"].value
        == ("Estimated actuals" if language == "en" else "Gerçekleşen tahmini")
    )
    assert sheet["D6"].value == sheet["E10"].value == ("Forecast" if language == "en" else "Tahmin")
    assert sheet["A5"].value == sheet["A9"].value == datetime(2026, 10, 25)
    assert sheet["A6"].value == sheet["A10"].value == datetime(2026, 10, 26)
    assert sheet["D9"].data_type == "n"
    assert sheet["E9"].data_type == "s"
    assert sheet["D4"].alignment.horizontal == sheet["D5"].alignment.horizontal == "left"
    assert sheet["D8"].alignment.horizontal == sheet["D9"].alignment.horizontal == "right"
    assert sheet["E8"].alignment.horizontal == sheet["E9"].alignment.horizontal == "left"
    assert sheet["E9"].fill.fgColor.rgb != sheet["E10"].fill.fgColor.rgb
    assert sheet["E9"].border.top.style == sheet["E10"].border.top.style == "medium"
    assert sheet.auto_filter.ref == "A8:E10"
    assert sheet.freeze_panes == "A9"


def test_mixed_workbook_preserves_partial_energy_unknown_outage_and_dst_sources() -> None:
    report = _report(outage=True, incomplete=True, dst=True)
    interval = replace(
        report.days[0].intervals[0],
        energy=1.25,
        outage_energy=None,
        source_kind="mixed",
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
    sheet = _sheet(replace(report, days=(day,)))
    assert sheet["B5"].value == sheet["F8"].value == 1.25
    assert sheet["D5"].value is None
    assert sheet["G8"].value is None
    assert sheet["F5"].value == "Partial estimate; Outage impact unknown"
    assert sheet["G4"].value == sheet["I7"].value == "Source"
    assert sheet["G5"].value == sheet["I8"].value == "Estimated + forecast"
    assert sheet["F7"].value == "Normal operation (MWh)"
    assert sheet["G7"].value == "With reported outage (MWh)"
    assert sheet["D8"].value == "UTC+02:00"
    assert sheet["E8"].value == "UTC+01:00"
    for column in range(1, 10):
        expected = "left" if column in (1, 8, 9) else "center" if column <= 5 else "right"
        assert sheet.cell(7, column).alignment.horizontal == expected
        assert sheet.cell(8, column).alignment.horizontal == expected
    assert sheet["I8"].alignment.wrap_text
    assert sheet.auto_filter.ref == "A7:I8"
