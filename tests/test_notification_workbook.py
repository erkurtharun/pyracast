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
    assert sheet["F5"].value == "Forecast partial"
    assert sheet["D7"].value == "Normal operation (MWh)"
    assert sheet["E7"].value == "With reported outage (MWh)"
    assert sheet["D8"].value is None
    assert sheet["E8"].value == 0
    assert sheet["F8"].value == "Forecast partial; Outage partial"


def test_workbook_disambiguates_clock_change() -> None:
    sheet = _sheet(_report(dst=True))
    assert sheet["B8"].value == sheet["C8"].value
    assert sheet["D8"].value == "UTC+02:00"
    assert sheet["E8"].value == "UTC+01:00"


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
        "Forecast unavailable; Outage impact unknown"
        if unknown
        else "Forecast unavailable; Outage unavailable"
    )
    assert sheet["B6"].value == 0
    assert sheet["D6"].value is None
    assert sheet["F6"].value is None
    assert sheet.row_dimensions[5].height == 30
