from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from django.utils import translation
from openpyxl import load_workbook
from openpyxl.cell.cell import Cell, MergedCell
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


TODAY, TOMORROW, DAY_THREE = date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)
INK, TINT, WHITE, NOTE = "09383A", "DDEBEA", "FFFFFF", "F1F5F9"


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


def _hours(*hours: int, outage: float | None = None) -> tuple[ReportInterval, ...]:
    return tuple(_interval(hour, 1.0, outage) for hour in hours)


def _shown(sheet: Worksheet, row: int, column: int) -> Cell | MergedCell:
    for merged in sheet.merged_cells.ranges:
        if merged.min_row == row and merged.min_col <= column <= merged.max_col:
            return sheet.cell(row, merged.min_col)
    return sheet.cell(row, column)


def _fill(sheet: Worksheet, row: int) -> str:
    cells = [_shown(sheet, row, column) for column in range(1, sheet.max_column + 1)]
    (fill,) = {
        cell.fill.fgColor.rgb[-6:] if cell.fill.fill_type == "solid" else "" for cell in cells
    }
    assert fill
    return fill


def _merged(sheet: Worksheet) -> set[str]:
    return {str(merged) for merged in sheet.merged_cells.ranges}


def _widths(sheet: Worksheet) -> list[float]:
    return [sheet.column_dimensions[letter].width for letter in sorted(sheet.column_dimensions)]


@pytest.mark.parametrize(
    ("language", "electrical", "name", "line", "label", "headings", "top", "columns"),
    [
        (
            "en",
            True,
            "Forecast",
            "Generation forecast · Pyracast",
            "Forecast (MWh)",
            ["Daily totals", "Hourly values"],
            ['"Forecast data: "dd mmm yyyy hh:mm', "Time zone", "Unit", "Date"],
            ["From", "To"],
        ),
        (
            "tr",
            True,
            "Tahmin",
            "Üretim tahmini · Pyracast",
            "Tahmin (MWh)",
            ["Günlük toplamlar", "Saatlik değerler"],
            ['"Tahmin verisi: "dd.mm.yyyy hh:mm', "Saat dilimi", "Birim", "Tarih"],
            ["Başlang\u0131ç", "Bitiş"],
        ),
        (
            "en",
            False,
            "Irradiance",
            "Solar irradiance forecast (GHI) · Pyracast",
            "Irradiance (kWh/m²)",
            ["Daily totals", "Interval values"],
            ['"Forecast data: "dd mmm yyyy hh:mm', "Time zone", "Unit", "Date"],
            ["From", "To"],
        ),
        (
            "tr",
            False,
            "Iş\u0131n\u0131m",
            "Güneş \u0131ş\u0131n\u0131m\u0131 tahmini (GHI) · Pyracast",
            "Iş\u0131n\u0131m (kWh/m²)",
            ["Günlük toplamlar", "Aral\u0131k değerleri"],
            ['"Tahmin verisi: "dd.mm.yyyy hh:mm', "Saat dilimi", "Birim", "Tarih"],
            ["Başlang\u0131ç", "Bitiş"],
        ),
    ],
)
def test_one_sheet_shows_the_daily_totals_and_under_them_a_block_per_day(
    language: str,
    electrical: bool,
    name: str,
    line: str,
    label: str,
    headings: list[str],
    top: list[str],
    columns: list[str],
) -> None:
    book = _book(_report(electrical=electrical), language)
    sheet = cast(Worksheet, book.active)
    day_format = "dd.mm.yyyy" if language == "tr" else "dd mmm yyyy"
    assert book.sheetnames == [name]
    assert book.properties.creator == "Pyracast"
    assert book.properties.title.startswith("=unsafe ")
    assert sheet["A1"].value == "=unsafe"
    assert sheet["A1"].data_type == "s"
    assert sheet["A1"].font.b
    assert sheet["A1"].font.sz == 14
    assert sheet["A2"].value == line
    assert sheet["A4"].value == datetime(2026, 10, 26, 14, 2)
    assert sheet["A4"].number_format == top[0]
    assert sheet["A5"].value == f"{top[1]}: Europe/Berlin (UTC+1)"
    assert sheet["A6"].value == f"{top[2]}: " + ("MWh" if electrical else "kWh/m²")
    assert [sheet.cell(row, 1).value for row in (3, 7, 8)] == [None, None, headings[0]]
    assert sheet["A8"].font.b
    assert [sheet.cell(9, column).value for column in (1, 2, 3)] == [top[3], None, label]
    assert [sheet.cell(row, 1).value for row in (10, 11)] == [
        datetime(2026, 10, 26),
        datetime(2026, 10, 27),
    ]
    assert sheet["C10"].value == 12.5
    assert sheet["C10"].number_format == "#,##0.00"
    assert sheet["C10"].data_type == "n"
    assert sheet["A10"].number_format == day_format
    assert [sheet.cell(row, 1).value for row in (12, 13, 14)] == [None, headings[1], None]
    assert sheet["A13"].font.b
    assert sheet["A15"].value == datetime(2026, 10, 26)
    assert sheet["A15"].number_format == day_format
    assert [sheet.cell(16, column).value for column in (1, 2, 3)] == [*columns, label]
    assert [sheet.cell(row, 1).value for row in (17, 18)] == [
        datetime(2026, 10, 26, 11),
        datetime(2026, 10, 26, 12),
    ]
    assert [sheet.cell(row, 1).value for row in (19, 20)] == [None, datetime(2026, 10, 27)]
    assert sheet.max_row == 21
    assert _merged(sheet) == {
        "A1:C1",
        "A2:C2",
        "A4:C4",
        "A5:C5",
        "A6:C6",
        "A8:C8",
        "A9:B9",
        "A10:B10",
        "A11:B11",
        "A13:C13",
        "A15:C15",
        "A20:C20",
    }
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
    assert [sheet.cell(9, column).value for column in (1, 3, 4)] == [
        "Date",
        "Forecast (MWh)",
        "Without outage (MWh)",
    ]
    assert [sheet.cell(10, column).value for column in (3, 4)] == [12.5, 12.5]
    assert [sheet.cell(11, column).value for column in (3, 4)] == [4.25, 12.5]
    assert sheet["A12"].value == "The Forecast column includes the recorded outage."
    assert [sheet.cell(row, 1).value for row in (13, 14)] == [None, "Hourly values"]
    assert sheet["A19"].value == datetime(2026, 10, 27)
    assert [sheet.cell(20, column).value for column in range(1, 5)] == [
        "From",
        "To",
        "Forecast (MWh)",
        "Without outage (MWh)",
    ]
    assert [sheet.cell(row, 3).value for row in (21, 22)] == [1.0, None]
    assert [sheet.cell(row, 4).value for row in (21, 22)] == [2.0, 3.0]
    assert sheet.auto_filter.ref is None
    assert _widths(sheet) == [9, 9, 12, 12]
    assert sheet.row_dimensions[20].height == 55


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
    assert {"A12:C12", "A13:C13", "A14:C14"} <= _merged(sheet)
    assert [_fill(sheet, row) for row in (12, 13, 14)] == [NOTE] * 3
    assert all(sheet.cell(row, 1).alignment.wrap_text for row in (12, 13, 14))
    assert sheet["D9"].value is None
    assert sheet["C10"].value == 12.5
    assert sheet["C11"].value is None
    missing = _day(TOMORROW, None, coverage=0.0)
    sheet = _sheet(_report(_day(TODAY), missing), language)
    assert sheet["A12"].value == (
        "Tue 27 Oct 2026: no forecast data."
        if language == "en"
        else "27 Ekim 2026 Sal\u0131: tahmin verisi yok."
    )
    assert sheet["A13"].value is None


def test_the_sheet_reads_at_a_glance_without_grid_filter_or_frozen_rows() -> None:
    partial = replace(_interval(11, 0.75), complete=False)
    sheet = _sheet(_report(_day(TODAY, intervals=(_interval(10, 0.0), partial)), _day(TOMORROW)))
    assert sheet.max_row == 21
    assert sheet.max_column == 3
    assert sheet.freeze_panes is None
    assert sheet.auto_filter.ref is None
    assert sheet.print_title_rows is None
    assert sheet.print_area == "'Forecast'!$A$1:$C$21"
    assert sheet.page_setup.fitToWidth == 1
    assert sheet.page_setup.fitToHeight == 0
    assert sheet.sheet_view.showGridLines is False
    assert sheet.sheet_view.showRowColHeaders is False
    assert sheet.sheet_view.zoomScale == 100
    assert _widths(sheet) == [9, 9, 24]
    for header in (sheet["A9"], sheet["A15"], sheet["A16"]):
        assert header.font.b
        assert header.font.color.rgb.endswith(WHITE)
        assert header.fill.fgColor.rgb.endswith(INK)
    assert sheet["A17"].value == datetime(2026, 10, 26, 11)
    assert sheet["B17"].value == datetime(2026, 10, 26, 12)
    assert sheet["A17"].number_format == sheet["B17"].number_format == "hh:mm"
    assert sheet["C17"].value == 0
    assert sheet["C17"].data_type == "n"
    assert sheet["C17"].number_format == "#,##0.000"
    assert sheet["C17"].font.color.rgb.endswith(INK)
    assert sheet["C18"].value is None
    for cell in (sheet["A5"], sheet["A10"], sheet["C10"], sheet["A17"], sheet["C17"]):
        assert cell.alignment.horizontal == "center"
        assert cell.alignment.wrap_text


def _clock_change_rows(day: date, first_hour: int) -> tuple[ReportInterval, ...]:
    start = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=first_hour)
    return tuple(
        replace(
            _interval(10, 1.0),
            start=start + timedelta(hours=hour),
            end=start + timedelta(hours=hour + 1),
        )
        for hour in range(2)
    )


def test_a_clock_change_day_shows_each_times_utc_offset_inside_the_time_cells() -> None:
    report = _report(
        _day(date(2026, 10, 25), intervals=_clock_change_rows(date(2026, 10, 25), 0)),
        _day(TODAY, intervals=_hours(10)),
    )
    sheet = _sheet(report, "tr")
    assert sheet["A17"].value == sheet["A18"].value == datetime(2026, 10, 25, 2)
    assert [sheet.cell(row, column).number_format for row in (17, 18) for column in (1, 2)] == [
        'hh:mm"\nUTC+2"',
        'hh:mm"\nUTC+1"',
        'hh:mm"\nUTC+1"',
        'hh:mm"\nUTC+1"',
    ]
    assert sheet.row_dimensions[17].height == 40
    assert sheet["A22"].value == datetime(2026, 10, 26, 11)
    assert sheet["A22"].number_format == sheet["B22"].number_format == "hh:mm"
    assert sheet.max_column == 3
    assert _widths(sheet) == [9, 9, 24]
    assert _sheet(_report()).max_column == 3


@pytest.mark.parametrize(
    ("day", "offset"),
    [(date(2026, 10, 25), "UTC+1"), (date(2026, 3, 29), "UTC+2")],
)
def test_a_clock_change_day_shows_the_offset_even_when_only_daylight_rows_are_exported(
    day: date, offset: str
) -> None:
    report = _report(
        _day(day, intervals=_clock_change_rows(day, 10)), _day(day + timedelta(days=1))
    )
    sheet = _sheet(report)
    assert [sheet.cell(row, 1).number_format for row in (17, 18)] == [f'hh:mm"\n{offset}"'] * 2


@pytest.mark.parametrize(
    ("name", "lines"),
    [
        ("A" * 100, 5),
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
    assert "A1:C1" in _merged(sheet)
    assert min(409, lines * 18) <= sheet.row_dimensions[1].height <= 409


def _title_height(name: str) -> float:
    return cast(float, _sheet(replace(_report(), plant_name=name)).row_dimensions[1].height)


def test_a_name_of_wide_letters_gets_a_taller_title_row_than_one_of_narrow_letters() -> None:
    assert _title_height("W" * 100) > _title_height("i" * 100)
    assert _title_height("W" * 100) >= 6 * 19


@pytest.mark.parametrize(
    ("name", "lines"),
    [
        ("KARAMAN GÜNEŞ ENERJİSİ ÜRETİM SANTRALİ MERKEZ", 2),
        ("WESTMOOR MEADOW WIND AND SOLAR POWER PARK", 2),
    ],
    ids=["turkish capitals", "english capitals"],
)
def test_a_long_name_in_capitals_gets_the_lines_it_needs(name: str, lines: int) -> None:
    assert _title_height(name) >= lines * 19


def test_a_long_word_in_capitals_breaks_between_whole_letters() -> None:
    assert _title_height("KARAMANMERKEZGÜNEŞENERJİSİGENELSANTRAL") == 3 * 19 + 4


def test_turkish_subtitle_and_missing_data_note_rows_stay_one_line_high() -> None:
    missing = _day(date(2026, 5, 10), None, coverage=0.0)
    sheet = _sheet(_report(_day(date(2026, 5, 9)), missing, electrical=False), "tr")
    assert sheet["A2"].value == "Güneş \u0131ş\u0131n\u0131m\u0131 tahmini (GHI) · Pyracast"
    assert sheet["A12"].value == "10 May\u0131s 2026 Pazar: tahmin verisi yok."
    assert sheet.row_dimensions[2].height == sheet.row_dimensions[12].height == 25


def test_a_short_name_keeps_a_one_line_title_row() -> None:
    assert _title_height("North plant") == 23
    assert _title_height("NORTH PLANT") == 23


def test_a_very_long_name_of_wide_letters_stops_at_the_tallest_row() -> None:
    assert _title_height("W" * 2000) == 409


def test_values_keep_full_precision_and_a_missing_value_is_never_zero() -> None:
    precise = _day(TODAY, 1234567.891234, intervals=(_interval(10, 1234.5678912),))
    sheet = _sheet(_report(precise, _day(TOMORROW, None, coverage=0)))
    assert sheet["C10"].value == 1234567.891234
    assert sheet["C11"].value is None
    assert sheet["C18"].value == 1234.5678912
    assert sheet["C18"].number_format == "#,##0.000"
    words = _texts(_book(_report(precise)))
    for word in ("Source", "Estimated", "Partial", "Coverage", "Updated", "Normal operation"):
        assert word not in words


@pytest.mark.parametrize(
    "days",
    [
        (_day(TODAY, intervals=_hours(10)), _day(TOMORROW, intervals=_hours(10))),
        (
            _day(TODAY, intervals=_hours(10)),
            _day(TOMORROW, outage_total=4.25, outage=True, intervals=_hours(10, outage=0.5)),
        ),
        (
            _day(date(2026, 10, 25), intervals=_clock_change_rows(date(2026, 10, 25), 0)),
            _day(TODAY, intervals=_hours(10)),
        ),
    ],
    ids=["plain", "with the outage column", "with a clock change"],
)
def test_the_sheet_is_42_characters_wide_in_every_variant(days: tuple[ReportDay, ...]) -> None:
    sheet = _sheet(_report(*days))
    assert sum(_widths(sheet)) == 42
    assert len(_widths(sheet)) == sheet.max_column
    assert sheet.auto_filter.ref is None
    assert "Date" not in [sheet.cell(row, 1).value for row in range(13, sheet.max_row + 1)]


def test_each_day_has_its_dated_heading_and_header_and_the_days_alternate_fills() -> None:
    second = (_interval(10, 1.0), _interval(11, None))
    sheet = _sheet(
        _report(
            _day(TODAY, intervals=_hours(10, 11)),
            _day(TOMORROW, intervals=second),
            _day(DAY_THREE, intervals=_hours(10, 11)),
        )
    )
    for heading, day in ((16, TODAY), (21, TOMORROW), (26, DAY_THREE)):
        assert sheet.cell(heading - 1, 1).value is None
        assert sheet.cell(heading, 1).value == datetime.combine(day, datetime.min.time())
        assert f"A{heading}:C{heading}" in _merged(sheet)
        assert [sheet.cell(heading + 1, column).value for column in (1, 2, 3)] == [
            "From",
            "To",
            "Forecast (MWh)",
        ]
        assert _fill(sheet, heading) == _fill(sheet, heading + 1) == INK
    assert [_fill(sheet, row) for row in (10, 11, 12)] == [WHITE, TINT, WHITE]
    assert [_fill(sheet, row) for row in (18, 19, 23, 24, 28, 29)] == [WHITE] * 2 + [TINT] * 2 + [
        WHITE
    ] * 2
    assert sheet["C24"].value is None
    assert sheet["C24"].fill.fgColor.rgb.endswith(TINT)
    assert sheet.max_row == 29


def test_a_single_day_has_one_block() -> None:
    sheet = _sheet(_report(_day(TODAY, intervals=_hours(9, 10, 11))))
    headings = [
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == datetime(2026, 10, 26)
    ]
    assert headings == [10, 14]
    assert [sheet.cell(15, column).value for column in (1, 2, 3)] == [
        "From",
        "To",
        "Forecast (MWh)",
    ]
    assert sheet.max_row == 18
    assert [_fill(sheet, row) for row in (10, 16, 17, 18)] == [WHITE] * 4
