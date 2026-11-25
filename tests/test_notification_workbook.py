from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from django.utils import translation
from openpyxl import load_workbook
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from pyracast.notifications.presentation import ForecastReport, ReportDay, ReportInterval
from pyracast.notifications.workbook import build_workbook

ZONE = ZoneInfo("Europe/Berlin")


def _interval(hour: int, energy: float | None, outage: float | None = None) -> ReportInterval:
    start = datetime(2026, 10, 26, hour, tzinfo=UTC)
    return ReportInterval(
        start=start,
        end=start + timedelta(hours=1),
        energy=energy,
        outage_energy=outage,
        complete=energy is not None,
        outage_complete=outage is not None,
    )


def _day(
    day: date,
    total: float | None = 12.5,
    *,
    coverage: float = 1.0,
    outage_total: float | None = None,
    outage: bool = False,
    status: str | None = None,
    intervals: tuple[ReportInterval, ...] = (),
) -> ReportDay:
    return ReportDay(
        day=day,
        total=total,
        partial=0.0,
        coverage=coverage,
        outage_total=outage_total,
        outage_partial=0.0,
        outage_coverage=1.0 if outage_total is not None else 0.0,
        has_outage=outage,
        intervals=intervals,
        outage_status=status,
    )


TODAY, TOMORROW = date(2026, 10, 26), date(2026, 10, 27)


def _report(*days: ReportDay, electrical: bool = True, stale: bool = False) -> ForecastReport:
    return ForecastReport(
        plant_name="=unsafe\x01",
        timezone="Europe/Berlin",
        collected_at=datetime(2026, 10, 26, 13, 2, tzinfo=UTC),
        prepared_at=datetime(2026, 10, 26, 13, 5, tzinfo=UTC),
        days=days
        or (_day(TODAY, intervals=(_interval(10, 0.0), _interval(11, None))), _day(TOMORROW)),
        unit="MWh" if electrical else "kWh/m²",
        electrical=electrical,
        stale=stale,
    )


def _book(report: ForecastReport, language: str = "en") -> Workbook:
    with translation.override(language):
        return load_workbook(BytesIO(build_workbook(report)))


def _sheets(report: ForecastReport, language: str = "en") -> tuple[Worksheet, Worksheet]:
    summary, intervals = _book(report, language).worksheets
    return cast(Worksheet, summary), cast(Worksheet, intervals)


def _texts(book: Workbook) -> str:
    return " ".join(str(cell.value) for sheet in book.worksheets for row in sheet for cell in row)


@pytest.mark.parametrize(
    ("language", "electrical", "names", "line", "label"),
    [
        ("en", True, ["Summary", "Hourly"], "Generation forecast · Pyracast", "Forecast (MWh)"),
        ("tr", True, ["Özet", "Saatlik"], "Üretim tahmini · Pyracast", "Tahmin (MWh)"),
        (
            "en",
            False,
            ["Summary", "Intervals"],
            "Solar irradiance forecast (GHI) · Pyracast",
            "Irradiance (kWh/m²)",
        ),
        (
            "tr",
            False,
            ["Özet", "Aral\u0131klar"],
            "Güneş \u0131ş\u0131n\u0131m\u0131 tahmini (GHI) · Pyracast",
            "Iş\u0131n\u0131m (kWh/m²)",
        ),
    ],
)
def test_the_summary_explains_the_file_to_a_reader_who_only_has_it(
    language: str, electrical: bool, names: list[str], line: str, label: str
) -> None:
    book = _book(_report(electrical=electrical), language)
    summary = cast(Worksheet, book.worksheets[0])
    assert book.sheetnames == names
    assert book.properties.creator == "Pyracast"
    assert book.properties.title.startswith("=unsafe ")
    assert summary["A1"].value == "=unsafe"
    assert summary["A1"].data_type == "s"
    assert summary["A1"].font.b
    assert summary["A1"].font.sz == 14
    assert summary["A2"].value == line
    assert summary["B4"].value == datetime(2026, 10, 26, 14, 2)
    assert summary["B5"].value == "Europe/Berlin (UTC+1)"
    assert summary["B6"].value == ("MWh" if electrical else "kWh/m²")
    assert summary["B8"].value == label
    assert summary["C8"].value is None
    assert [summary.cell(row, 1).value for row in (9, 10)] == [
        datetime(2026, 10, 26),
        datetime(2026, 10, 27),
    ]
    assert summary["B9"].value == 12.5
    assert summary["B9"].number_format == "#,##0.00"
    assert summary["B9"].data_type == "n"
    assert summary["A9"].number_format == ("dd.mm.yyyy" if language == "tr" else "dd mmm yyyy")
    assert summary["A12"].value is None
    assert not summary.merged_cells.ranges
    assert not any(
        isinstance(cell.value, str) and cell.value.startswith("=")
        for sheet in book.worksheets
        for row in sheet
        for cell in row
        if cell.coordinate != "A1"
    )


def test_a_calculated_outage_adds_the_figure_without_it_and_says_so() -> None:
    affected = _day(
        TOMORROW,
        outage_total=4.25,
        outage=True,
        status="estimated",
        intervals=(_interval(10, 2.0, 1.0), _interval(11, 3.0, None)),
    )
    summary, hourly = _sheets(_report(_day(TODAY), affected))
    assert [summary.cell(8, column).value for column in (1, 2, 3)] == [
        "Date",
        "Forecast (MWh)",
        "Without outage (MWh)",
    ]
    assert [summary.cell(9, column).value for column in (2, 3)] == [12.5, 12.5]
    assert [summary.cell(10, column).value for column in (2, 3)] == [4.25, 12.5]
    assert summary["A12"].value == "The Forecast column includes the recorded outage."
    assert [hourly.cell(1, column).value for column in range(1, 6)] == [
        "Date",
        "From",
        "To",
        "Forecast (MWh)",
        "Without outage (MWh)",
    ]
    assert [hourly.cell(row, 4).value for row in (2, 3)] == [1.0, None]
    assert [hourly.cell(row, 5).value for row in (2, 3)] == [2.0, 3.0]


@pytest.mark.parametrize("language", ["en", "tr"])
def test_each_day_without_a_total_or_with_an_excluded_outage_has_one_note(language: str) -> None:
    excluded = _day(TODAY, outage=True, status="unknown_impact", outage_total=3.0)
    incomplete = _day(TOMORROW, None, coverage=0.5)
    summary, _ = _sheets(_report(excluded, incomplete, stale=True), language)
    notes = {
        "en": [
            "Newer forecast data was not available.",
            "Mon 26 Oct 2026: the recorded outage is not included.",
            "Tue 27 Oct 2026: data is missing for some hours; no daily total.",
        ],
        "tr": [
            "Daha yeni tahmin verisi al\u0131namad\u0131.",
            "26 Ekim 2026 Pazartesi: kay\u0131tl\u0131 kesinti dahil değildir.",
            "27 Ekim 2026 Sal\u0131: baz\u0131 saatlerin verisi eksik; günlük toplam verilmedi.",
        ],
    }[language]
    assert [summary.cell(row, 1).value for row in (12, 13, 14)] == notes
    assert summary["C8"].value is None
    assert summary["B9"].value == 12.5
    assert summary["B10"].value is None
    missing = _day(TOMORROW, None, coverage=0.0)
    summary, _ = _sheets(_report(_day(TODAY), missing), language)
    assert summary["A12"].value == (
        "Tue 27 Oct 2026: no forecast data."
        if language == "en"
        else "27 Ekim 2026 Sal\u0131: tahmin verisi yok."
    )
    assert summary["A13"].value is None


def test_the_hourly_sheet_is_a_clean_filterable_table() -> None:
    partial = replace(_interval(11, 0.75), complete=False)
    _, hourly = _sheets(
        _report(_day(TODAY, intervals=(_interval(10, 0.0), partial)), _day(TOMORROW))
    )
    assert hourly.max_row == 3
    assert hourly.max_column == 4
    assert hourly.freeze_panes == "A2"
    assert hourly.auto_filter.ref == "A1:D3"
    assert hourly.print_title_rows == "$1:$1"
    assert hourly.page_setup.fitToWidth == 1
    assert hourly.sheet_view.showGridLines is not False
    assert hourly.sheet_view.showRowColHeaders is not False
    assert not hourly.merged_cells.ranges
    header = hourly["A1"]
    assert header.font.b
    assert header.font.color.rgb.endswith("FFFFFF")
    assert header.fill.fgColor.rgb.endswith("09383A")
    assert hourly["A2"].value == datetime(2026, 10, 26)
    assert hourly["B2"].value == datetime(2026, 10, 26, 11)
    assert hourly["C2"].value == datetime(2026, 10, 26, 12)
    assert hourly["B2"].number_format == "hh:mm"
    assert hourly["D2"].value == 0
    assert hourly["D2"].data_type == "n"
    assert hourly["D2"].number_format == "#,##0.000"
    assert hourly["D2"].alignment.horizontal == "right"
    assert hourly["A1"].alignment.horizontal == "left"
    assert hourly["D3"].value is None


def test_a_clock_change_adds_each_rows_utc_offset() -> None:
    start = datetime(2026, 10, 25, 0, tzinfo=UTC)
    rows = tuple(
        replace(
            _interval(10, 1.0),
            start=start + timedelta(hours=hour),
            end=start + timedelta(hours=hour + 1),
        )
        for hour in range(2)
    )
    report = _report(_day(date(2026, 10, 25), intervals=rows), _day(TODAY))
    _, hourly = _sheets(report, "tr")
    assert hourly["E1"].value == "UTC fark\u0131"
    assert [hourly.cell(row, 5).value for row in (2, 3)] == ["UTC+2", "UTC+1"]
    assert hourly["B2"].value == hourly["B3"].value == datetime(2026, 10, 25, 2)
    assert all(hourly.cell(row, 2).number_format == "hh:mm" for row in (2, 3))
    _, plain = _sheets(_report())
    assert plain["E1"].value is None


@pytest.mark.parametrize(
    ("day", "offset"),
    [(date(2026, 10, 25), "UTC+1"), (date(2026, 3, 29), "UTC+2")],
)
def test_a_clock_change_day_adds_the_offset_even_when_only_daylight_rows_are_exported(
    day: date, offset: str
) -> None:
    start = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=10)
    rows = tuple(
        replace(
            _interval(10, 1.0),
            start=start + timedelta(hours=hour),
            end=start + timedelta(hours=hour + 1),
        )
        for hour in range(2)
    )
    report = _report(_day(day, intervals=rows), _day(day + timedelta(days=1)))
    _, hourly = _sheets(report)
    assert hourly["E1"].value == "UTC offset"
    assert [hourly.cell(row, 5).value for row in (2, 3)] == [offset, offset]


@pytest.mark.parametrize(
    ("name", "lines"),
    [
        ("A" * 100, 7),
        ("AAAA\n" * 19 + "AAAAA", 20),
        ("North plant\n\nEast", 3),
        ("\n" * 99 + "A", 100),
    ],
    ids=["one long line", "twenty short lines", "an empty line", "more lines than a row holds"],
)
def test_a_long_plant_name_wraps_in_a_row_tall_enough_to_show_it(name: str, lines: int) -> None:
    summary, _ = _sheets(replace(_report(), plant_name=name))
    assert summary["A1"].value == name
    assert summary["A1"].alignment.wrap_text
    assert summary["A1"].alignment.vertical == "top"
    assert min(409, lines * 18) <= summary.row_dimensions[1].height <= 409


def test_values_keep_full_precision_and_a_missing_value_is_never_zero() -> None:
    precise = _day(TODAY, 1234567.891234, intervals=(_interval(10, 1234.5678912),))
    summary, hourly = _sheets(_report(precise, _day(TOMORROW, None, coverage=0)))
    assert summary["B9"].value == 1234567.891234
    assert summary["B10"].value is None
    assert hourly["D2"].value == 1234.5678912
    assert hourly["D2"].number_format == "#,##0.000"
    words = _texts(_book(_report(precise)))
    for word in ("Source", "Estimated", "Partial", "Coverage", "Updated"):
        assert word not in words
