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


def _sheet(report: ForecastReport, language: str = "en") -> Worksheet:
    (sheet,) = _book(report, language).worksheets
    return cast(Worksheet, sheet)


def _texts(book: Workbook) -> str:
    return " ".join(str(cell.value) for sheet in book.worksheets for row in sheet for cell in row)


@pytest.mark.parametrize(
    ("language", "electrical", "name", "line", "label", "headings", "columns"),
    [
        (
            "en",
            True,
            "Forecast",
            "Generation forecast · Pyracast",
            "Forecast (MWh)",
            ["Daily totals", "Hourly values"],
            ["Date", "From", "To"],
        ),
        (
            "tr",
            True,
            "Tahmin",
            "Üretim tahmini · Pyracast",
            "Tahmin (MWh)",
            ["Günlük toplamlar", "Saatlik değerler"],
            ["Tarih", "Başlang\u0131ç", "Bitiş"],
        ),
        (
            "en",
            False,
            "Irradiance",
            "Solar irradiance forecast (GHI) · Pyracast",
            "Irradiance (kWh/m²)",
            ["Daily totals", "Interval values"],
            ["Date", "From", "To"],
        ),
        (
            "tr",
            False,
            "Iş\u0131n\u0131m",
            "Güneş \u0131ş\u0131n\u0131m\u0131 tahmini (GHI) · Pyracast",
            "Iş\u0131n\u0131m (kWh/m²)",
            ["Günlük toplamlar", "Aral\u0131k değerleri"],
            ["Tarih", "Başlang\u0131ç", "Bitiş"],
        ),
    ],
)
def test_one_sheet_shows_the_daily_totals_and_under_them_the_hourly_values(
    language: str,
    electrical: bool,
    name: str,
    line: str,
    label: str,
    headings: list[str],
    columns: list[str],
) -> None:
    book = _book(_report(electrical=electrical), language)
    sheet = cast(Worksheet, book.active)
    assert book.sheetnames == [name]
    assert book.properties.creator == "Pyracast"
    assert book.properties.title.startswith("=unsafe ")
    assert sheet["A1"].value == "=unsafe"
    assert sheet["A1"].data_type == "s"
    assert sheet["A1"].font.b
    assert sheet["A1"].font.sz == 14
    assert sheet["A2"].value == line
    assert sheet["B4"].value == datetime(2026, 10, 26, 14, 2)
    assert sheet["B5"].value == "Europe/Berlin (UTC+1)"
    assert sheet["B6"].value == ("MWh" if electrical else "kWh/m²")
    assert [sheet.cell(row, 1).value for row in (7, 8)] == [None, headings[0]]
    assert sheet["A8"].font.b
    assert sheet["B9"].value == label
    assert sheet["C9"].value is None
    assert [sheet.cell(row, 1).value for row in (10, 11)] == [
        datetime(2026, 10, 26),
        datetime(2026, 10, 27),
    ]
    assert sheet["B10"].value == 12.5
    assert sheet["B10"].number_format == "#,##0.00"
    assert sheet["B10"].data_type == "n"
    assert sheet["A10"].number_format == ("dd.mm.yyyy" if language == "tr" else "dd mmm yyyy")
    assert [sheet.cell(row, 1).value for row in (12, 13)] == [None, headings[1]]
    assert sheet["A13"].font.b
    assert [sheet.cell(14, column).value for column in (1, 2, 3, 4)] == [*columns, label]
    assert [sheet.cell(row, 1).value for row in (15, 16)] == [datetime(2026, 10, 26)] * 2
    assert sheet.max_row == 16
    assert not sheet.merged_cells.ranges
    assert not any(
        isinstance(cell.value, str) and cell.value.startswith("=")
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
    sheet = _sheet(_report(_day(TODAY), affected))
    assert [sheet.cell(9, column).value for column in (1, 2, 3)] == [
        "Date",
        "Forecast (MWh)",
        "Without outage (MWh)",
    ]
    assert [sheet.cell(10, column).value for column in (2, 3)] == [12.5, 12.5]
    assert [sheet.cell(11, column).value for column in (2, 3)] == [4.25, 12.5]
    assert sheet["A12"].value == "The Forecast column includes the recorded outage."
    assert [sheet.cell(row, 1).value for row in (13, 14)] == [None, "Hourly values"]
    assert [sheet.cell(15, column).value for column in range(1, 6)] == [
        "Date",
        "From",
        "To",
        "Forecast (MWh)",
        "Without outage (MWh)",
    ]
    assert [sheet.cell(row, 4).value for row in (16, 17)] == [1.0, None]
    assert [sheet.cell(row, 5).value for row in (16, 17)] == [2.0, 3.0]
    assert sheet.auto_filter.ref == "A15:E17"
    assert [sheet.column_dimensions[letter].width for letter in "ABCDE"] == [26, 20, 24, 20, 24]


@pytest.mark.parametrize("language", ["en", "tr"])
def test_each_day_without_a_total_or_with_an_excluded_outage_has_one_note(language: str) -> None:
    excluded = _day(TODAY, outage=True, status="unknown_impact", outage_total=3.0)
    incomplete = _day(TOMORROW, None, coverage=0.5)
    sheet = _sheet(_report(excluded, incomplete, stale=True), language)
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
    assert [sheet.cell(row, 1).value for row in (12, 13, 14, 15)] == [*notes, None]
    assert sheet["C9"].value is None
    assert sheet["B10"].value == 12.5
    assert sheet["B11"].value is None
    missing = _day(TOMORROW, None, coverage=0.0)
    sheet = _sheet(_report(_day(TODAY), missing), language)
    assert sheet["A12"].value == (
        "Tue 27 Oct 2026: no forecast data."
        if language == "en"
        else "27 Ekim 2026 Sal\u0131: tahmin verisi yok."
    )
    assert sheet["A13"].value is None


def test_the_hourly_values_are_a_clean_filterable_table() -> None:
    partial = replace(_interval(11, 0.75), complete=False)
    sheet = _sheet(_report(_day(TODAY, intervals=(_interval(10, 0.0), partial)), _day(TOMORROW)))
    assert sheet.max_row == 16
    assert sheet.max_column == 4
    assert sheet.freeze_panes is None
    assert sheet.auto_filter.ref == "A14:D16"
    assert sheet.print_title_rows is None
    assert sheet.page_setup.fitToWidth == 1
    assert sheet.page_setup.fitToHeight == 0
    assert sheet.sheet_view.showGridLines is not False
    assert sheet.sheet_view.showRowColHeaders is not False
    assert not sheet.merged_cells.ranges
    assert [sheet.column_dimensions[letter].width for letter in "ABCD"] == [26, 20, 8, 20]
    for header in (sheet["A9"], sheet["A14"]):
        assert header.font.b
        assert header.font.color.rgb.endswith("FFFFFF")
        assert header.fill.fgColor.rgb.endswith("09383A")
        assert header.alignment.horizontal == "left"
    assert sheet["A15"].value == datetime(2026, 10, 26)
    assert sheet["B15"].value == datetime(2026, 10, 26, 11)
    assert sheet["C15"].value == datetime(2026, 10, 26, 12)
    assert sheet["B15"].number_format == "hh:mm"
    assert sheet["D15"].value == 0
    assert sheet["D15"].data_type == "n"
    assert sheet["D15"].number_format == "#,##0.000"
    assert sheet["D15"].alignment.horizontal == "right"
    assert sheet["D16"].value is None


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
    sheet = _sheet(report, "tr")
    assert sheet["E14"].value == "UTC fark\u0131"
    assert [sheet.cell(row, 5).value for row in (15, 16)] == ["UTC+2", "UTC+1"]
    assert sheet["B15"].value == sheet["B16"].value == datetime(2026, 10, 25, 2)
    assert all(sheet.cell(row, 2).number_format == "hh:mm" for row in (15, 16))
    assert sheet.column_dimensions["E"].width == 12
    assert _sheet(_report()).max_column == 4


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
    sheet = _sheet(report)
    assert sheet["E14"].value == "UTC offset"
    assert [sheet.cell(row, 5).value for row in (15, 16)] == [offset, offset]


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
    sheet = _sheet(replace(_report(), plant_name=name))
    assert sheet["A1"].value == name
    assert sheet["A1"].alignment.wrap_text
    assert sheet["A1"].alignment.vertical == "top"
    assert min(409, lines * 18) <= sheet.row_dimensions[1].height <= 409


def test_values_keep_full_precision_and_a_missing_value_is_never_zero() -> None:
    precise = _day(TODAY, 1234567.891234, intervals=(_interval(10, 1234.5678912),))
    sheet = _sheet(_report(precise, _day(TOMORROW, None, coverage=0)))
    assert sheet["B10"].value == 1234567.891234
    assert sheet["B11"].value is None
    assert sheet["D16"].value == 1234.5678912
    assert sheet["D16"].number_format == "#,##0.000"
    words = _texts(_book(_report(precise)))
    for word in ("Source", "Estimated", "Partial", "Coverage", "Updated"):
        assert word not in words


DAY_THREE = date(2026, 10, 28)
PLAIN = (None, None, None)
BAND = ("DDEBEA", None, None)
PLAIN_START = (None, "medium", "09383A")
BAND_START = ("DDEBEA", "medium", "09383A")


def _hours(*hours: int, outage: float | None = None) -> tuple[ReportInterval, ...]:
    return tuple(_interval(hour, 1.0, outage) for hour in hours)


def _look(sheet: Worksheet, row: int, width: int) -> tuple[str | None, str | None, str | None]:
    looks = set()
    for column in range(1, width + 1):
        cell = sheet.cell(row, column)
        top = cell.border.top
        looks.add(
            (
                cell.fill.fgColor.rgb[-6:] if cell.fill.fill_type == "solid" else None,
                top.style,
                top.color.rgb[-6:] if top.style else None,
            )
        )
    (look,) = looks
    return look


def _looks(sheet: Worksheet, rows: range, width: int) -> list[tuple[str | None, ...]]:
    return [_look(sheet, row, width) for row in rows]


def test_the_second_day_is_banded_and_begins_under_a_line_while_a_lone_day_has_neither() -> None:
    lone = _sheet(_report(_day(TODAY, intervals=_hours(9, 10, 11))))
    assert lone.auto_filter.ref == "A13:D16"
    assert _looks(lone, range(10, 11), 2) == [PLAIN]
    assert _looks(lone, range(14, 17), 4) == [PLAIN] * 3
    sheet = _sheet(
        _report(_day(TODAY, intervals=_hours(9, 10, 11)), _day(TOMORROW, intervals=_hours(9, 10)))
    )
    assert sheet.auto_filter.ref == "A14:D19"
    assert not sheet.merged_cells.ranges
    assert sheet.max_row == 19
    assert _looks(sheet, range(10, 12), 2) == [PLAIN, BAND]
    assert _looks(sheet, range(15, 20), 4) == [PLAIN, PLAIN, PLAIN, BAND_START, BAND]
    assert sheet["E18"].fill.fill_type is None


def test_a_third_day_returns_to_no_band_and_begins_under_a_line() -> None:
    sheet = _sheet(
        _report(
            _day(TODAY, intervals=_hours(10, 11)),
            _day(TOMORROW, intervals=_hours(10, 11)),
            _day(DAY_THREE, intervals=_hours(10, 11)),
        )
    )
    assert sheet.auto_filter.ref == "A15:D21"
    assert not sheet.merged_cells.ranges
    assert _looks(sheet, range(10, 13), 2) == [PLAIN, BAND, PLAIN]
    assert _looks(sheet, range(16, 22), 4) == [
        PLAIN,
        PLAIN,
        BAND_START,
        BAND,
        PLAIN_START,
        PLAIN,
    ]


def test_the_band_and_the_line_span_the_outage_column() -> None:
    affected = _day(
        TOMORROW,
        outage_total=4.25,
        outage=True,
        status="estimated",
        intervals=_hours(10, 11, outage=0.5),
    )
    sheet = _sheet(_report(_day(TODAY, intervals=_hours(10, 11)), affected))
    assert sheet.auto_filter.ref == "A15:E19"
    assert _looks(sheet, range(10, 12), 3) == [PLAIN, BAND]
    assert _looks(sheet, range(16, 20), 5) == [PLAIN, PLAIN, BAND_START, BAND]
    assert sheet["F18"].fill.fill_type is None


def test_the_band_and_the_line_span_the_utc_offset_column() -> None:
    sheet = _sheet(
        _report(
            _day(date(2026, 10, 25), intervals=_hours(10, 11)),
            _day(TODAY, intervals=_hours(10, 11)),
        )
    )
    assert sheet["E14"].value == "UTC offset"
    assert sheet.auto_filter.ref == "A14:E18"
    assert _looks(sheet, range(15, 19), 5) == [PLAIN, PLAIN, BAND_START, BAND]
