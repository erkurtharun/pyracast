import re
from base64 import b64decode
from datetime import UTC, date, datetime, timedelta, timezone
from io import BytesIO
from math import nextafter
from typing import Any, cast
from zipfile import ZipFile

import pytest
from django.utils import translation
from openpyxl import load_workbook

from pyracast.notifications.formats import energy_number, file_name, utc_offset
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.presentation import present_report
from pyracast.notifications.rendering import ForecastMail, render_forecast

START = datetime(2026, 11, 15, 8, tzinfo=UTC)
PLANT_URL = "https://testserver/app/w/3/plants/9/forecast/"
SOURCE_WORDS = ("Estimated", "estimate", "Source", "Gerçekleşen", "öngörü", "Kaynak")


def interval(hour: int, energy: float | None, coverage: float = 1) -> dict[str, object]:
    return {
        "start": (START + timedelta(hours=hour)).isoformat(),
        "end": (START + timedelta(hours=hour + 1)).isoformat(),
        "energy_mwh": energy,
        "partial_energy_mwh": energy or 0,
        "coverage_fraction": coverage,
        "complete": energy is not None,
        "ghi_wm2": None if energy is None else energy * 1000,
    }


def payload(*, missing: bool = False, total: float = 2.5) -> dict[str, object]:
    rows = [interval(0, total), interval(1, None if missing else 0, 0 if missing else 1)]
    return {
        "intervals": rows,
        "daylight": {
            "energy_mwh": None if missing else total,
            "partial_energy_mwh": total,
            "energy_kwh_m2": None if missing else total,
            "partial_energy_kwh_m2": total,
            "coverage_fraction": 0.5 if missing else 1.0,
            "windows": [
                {"start": START.isoformat(), "end": (START + timedelta(hours=2)).isoformat()}
            ],
        },
    }


def empty() -> dict[str, object]:
    data = payload(missing=True)
    cast(dict[str, object], data["daylight"]).update(partial_energy_mwh=0, coverage_fraction=0)
    return data


def day_row(
    day: str,
    normal: dict[str, object] | None,
    outage: dict[str, object] | None = None,
    status: str | None = None,
) -> dict[str, object]:
    return {"date": day, "normal": normal, "outage": outage, "outage_status": status}


def context(
    normal: dict[str, object] | None,
    outage: dict[str, object] | None = None,
    *,
    today: dict[str, object] | None = None,
    status: str | None = None,
) -> dict[str, object]:
    return {
        "plant_id": 1,
        "plant_name": "Example solar plant",
        "timezone": "Europe/Istanbul",
        "collected_at": START.isoformat(),
        "days": [
            day_row("2026-11-15", today if today is not None else payload(total=1.25)),
            day_row("2026-11-16", normal, outage, status if outage else None),
        ],
    }


def rendered(
    source: dict[str, object],
    language: str = "en",
    *,
    results: list[dict[str, object] | None] | None = None,
    prepared_at: datetime = START,
    **mail: Any,
) -> dict[str, str]:
    with translation.override(language):
        return render_forecast(
            NotificationDelivery(scheduled_for=START),
            results or [None, None],
            prepared_at=prepared_at,
            report_context=source,
            mail=ForecastMail(footer="Footer text", plant_url=PLANT_URL, **mail),
        )


def workbook_text(message: dict[str, str]) -> str:
    book = load_workbook(BytesIO(b64decode(message["attachment_base64"])))
    return " ".join(str(cell.value) for sheet in book.worksheets for row in sheet for cell in row)


def test_electricity_report_distinguishes_known_zero_and_missing_outage() -> None:
    normal, outage = payload(), payload(missing=True)
    normal["report_intervals"] = normal["intervals"]
    report = present_report(
        context(normal, outage), [None, None], prepared_at=START, missed_collection=False
    )
    day = report.days[1]
    assert report.unit == "MWh"
    assert day.day == date(2026, 11, 16)
    assert day.total == 2.5
    assert day.outage_total is None
    assert day.outage_partial == 2.5
    assert day.coverage == 1
    assert day.outage_coverage == 0.5
    assert day.intervals[1].energy == 0
    assert day.intervals[1].outage_energy is None
    assert day.intervals[1].complete
    assert not day.intervals[1].outage_complete
    assert day.has_outage


def test_unavailable_electricity_is_not_substituted_with_irradiance() -> None:
    source = context(None)
    source["collected_at"] = None
    report = present_report(
        source, [payload(), payload()], prepared_at=START, missed_collection=True
    )
    assert report.electrical
    assert report.collected_at is None
    assert not report.stale
    assert report.days[1].total is None
    assert report.days[1].coverage is None
    assert report.days[1].intervals == ()


def test_regional_irradiance_uses_native_daylight_rows_and_explicit_units() -> None:
    source = context(None)
    source["plant_id"] = None
    data = payload()
    data["intervals"] = [
        interval(-1, 9),
        *cast(list[dict[str, object]], data["intervals"]),
        interval(2, 9),
    ]
    report = present_report(source, [None, data], prepared_at=START, missed_collection=False)
    day = report.days[1]
    assert report.unit == "kWh/m²"
    assert not report.electrical
    assert [item.energy for item in day.intervals] == [2.5, 0]
    assert sum(item.energy or 0 for item in day.intervals) == day.total


def test_missing_irradiance_is_a_gap_and_legacy_unknown_daylight_is_preserved() -> None:
    source = context(None)
    source["plant_id"] = None
    report = present_report(
        source, [None, payload(missing=True)], prepared_at=START, missed_collection=False
    )
    assert report.days[1].intervals[1].energy is None
    assert not report.days[1].intervals[1].complete
    old = {"intervals": [interval(0, 2.5)], "energy_kwh_m2": 2.5}
    report = present_report(source, [None, old], prepared_at=START, missed_collection=False)
    assert report.days[1].total == 2.5
    assert len(report.days[1].intervals) == 1


def test_partial_interval_preserves_covered_energy_without_claiming_completeness() -> None:
    data = payload(missing=True)
    row = cast(list[dict[str, object]], data["intervals"])[1]
    row.update(partial_energy_mwh=1.25, coverage_fraction=0.5)
    report = present_report(context(data), [None, None], prepared_at=START, missed_collection=False)
    assert report.days[1].intervals[1].energy == 1.25
    assert not report.days[1].intervals[1].complete


@pytest.mark.parametrize(
    ("age", "missed", "stale"),
    [
        (timedelta(hours=20), False, False),
        (timedelta(hours=26), False, False),
        (timedelta(hours=26, seconds=1), False, True),
        (timedelta(minutes=5), True, True),
    ],
)
def test_the_data_is_not_current_after_26_hours_or_when_the_slot_missed_its_collection(
    age: timedelta, missed: bool, stale: bool
) -> None:
    report = present_report(
        context(payload()), [None, None], prepared_at=START + age, missed_collection=missed
    )
    assert report.stale is stale


@pytest.mark.parametrize("language", ["en", "tr"])
def test_the_data_time_is_tomorrows_forecast_collection_not_todays_sources(
    language: str,
) -> None:
    today = payload()
    today["provenance"] = {
        "source_kind": "mixed",
        "collected_at": (START + timedelta(minutes=1)).isoformat(),
        "oldest_collected_at": (START - timedelta(hours=30)).isoformat(),
        "source_snapshots": [
            {
                "snapshot_id": 13,
                "source_kind": "estimated_actuals",
                "collected_at": (START + timedelta(minutes=1)).isoformat(),
            },
            {
                "snapshot_id": 11,
                "source_kind": "forecast",
                "collected_at": (START - timedelta(hours=30)).isoformat(),
            },
        ],
    }
    message = rendered(context(payload(), today=today), language, prepared_at=START)
    line = {
        "en": "Forecast data: 15 Nov 2026, 11:00 (UTC+3)",
        "tr": "Tahmin verisi: 15 Kas\u0131m 2026, 11:00 (UTC+3)",
    }[language]
    for part in (message["body"], message["html"]):
        assert line in part
        assert "11:01" not in part
        assert ("Newer" if language == "en" else "Daha yeni") not in part
    assert message["body"].count(":00 (UTC") == 1
    assert "Updated" not in message["body"] + message["html"]


@pytest.mark.parametrize("language", ["en", "tr"])
def test_a_stale_forecast_puts_one_sentence_above_the_data_line_in_one_notice(
    language: str,
) -> None:
    message = rendered(context(payload()), language, prepared_at=START + timedelta(hours=27))
    sentence, line = {
        "en": ("Newer forecast data was not available.", "Forecast data: 15 Nov 2026, 11:00"),
        "tr": (
            "Daha yeni tahmin verisi al\u0131namad\u0131.",
            "Tahmin verisi: 15 Kas\u0131m 2026, 11:00",
        ),
    }[language]
    assert '<td class="mail-notice"' in message["html"]
    assert f"<strong>{sentence}</strong><br>{line} (UTC+3)</td>" in message["html"]
    assert f"{sentence}\n{line} (UTC+3)\n" in message["body"]
    assert sentence in workbook_text(message)


@pytest.mark.parametrize("source_kind", ["estimated_actuals", "mixed"])
@pytest.mark.parametrize("language", ["en", "tr"])
def test_no_part_of_the_mail_says_how_a_figure_is_composed(source_kind: str, language: str) -> None:
    today = payload()
    today["provenance"] = {"source_kind": source_kind}
    cast(list[dict[str, object]], today["intervals"])[0]["source_kinds"] = [
        "estimated_actuals",
        "forecast",
    ]
    message = rendered(context(payload(), today=today), language)
    for text in (message["body"], message["html"], workbook_text(message)):
        for word in SOURCE_WORDS:
            assert word not in text
    word = "Forecast" if language == "en" else "Tahmin"
    assert f"{word} (MWh)" in workbook_text(message)


@pytest.mark.parametrize(
    ("language", "tomorrow", "today", "number", "words"),
    [
        (
            "en",
            "Tomorrow · Mon 16 Nov 2026",
            "Today · Sun 15 Nov 2026",
            "1,284.25",
            ("Tomorrow (Mon 16 Nov 2026): 1,284.25 MWh", "Today (Sun 15 Nov 2026): 1.25 MWh"),
        ),
        (
            "tr",
            "Yar\u0131n · 16 Kas\u0131m 2026 Pazartesi",
            "Bugün · 15 Kas\u0131m 2026 Pazar",
            "1.284,25",
            (
                "Yar\u0131n (16 Kas\u0131m 2026 Pazartesi): 1.284,25 MWh",
                "Bugün (15 Kas\u0131m 2026 Pazar): 1,25 MWh",
            ),
        ),
    ],
)
def test_tomorrow_leads_with_two_grouped_decimals_and_today_follows(
    language: str, tomorrow: str, today: str, number: str, words: tuple[str, str]
) -> None:
    message = rendered(context(payload(total=1284.254)), language)
    html, body = message["html"], message["body"]
    assert html.index(tomorrow) < html.index(today)
    font = "font-family:Arial,Helvetica,sans-serif;"
    assert (
        f'>{number}&nbsp;<span style="{font}font-size:18px;font-weight:normal;">MWh</span>' in html
    )
    assert f'<span style="{font}font-size:15px;font-weight:normal;">MWh</span>' in html
    assert body.index(words[0]) < body.index(words[1])
    assert html.count("<h1") == 1
    assert ">Example solar plant</h1>" in html


@pytest.mark.parametrize(
    ("language", "preheader"),
    [
        ("en", "Tomorrow 2.50\u00a0MWh · Today 1.25\u00a0MWh"),
        ("tr", "Yar\u0131n 2,50\u00a0MWh · Bugün 1,25\u00a0MWh"),
    ],
)
def test_the_preheader_carries_both_totals_and_hides_the_rest_of_the_preview(
    language: str, preheader: str
) -> None:
    html = rendered(context(payload()), language)["html"]
    hidden = html.index('<div aria-hidden="true"')
    assert html.index("<body") < hidden < html.index("<table")
    assert f">{preheader}&#847;&zwnj;&nbsp;&#847;" in html


@pytest.mark.parametrize(
    ("tomorrow", "today", "language", "preheader"),
    [
        ("incomplete", "figure", "en", ">Tomorrow&#x27;s forecast is incomplete · Today 2.50"),
        ("missing", "figure", "en", ">No forecast for tomorrow · Today 2.50"),
        ("figure", "missing", "en", ">Tomorrow 2.50\u00a0MWh&#847;"),
        ("incomplete", "missing", "tr", ">Yar\u0131n\u0131n tahmini eksik&#847;"),
        ("missing", "incomplete", "tr", ">Yar\u0131n\u0131n tahmini haz\u0131rlanamad\u0131&#847;"),
    ],
)
def test_the_preheader_leaves_out_a_day_without_a_total(
    tomorrow: str, today: str, language: str, preheader: str
) -> None:
    days = {"figure": payload(), "incomplete": payload(missing=True), "missing": empty()}
    message = rendered(context(days[tomorrow], today=days[today]), language)
    assert preheader in message["html"]


@pytest.mark.parametrize(
    ("kind", "language", "subject"),
    [
        ("usual", "en", "Example solar plant: forecast for Mon 16 Nov"),
        ("usual", "tr", "Example solar plant: 16 Kas\u0131m Pazartesi üretim tahmini"),
        ("irradiance", "en", "Example solar plant: irradiance forecast for Mon 16 Nov"),
        (
            "irradiance",
            "tr",
            "Example solar plant: 16 Kas\u0131m Pazartesi \u0131ş\u0131n\u0131m tahmini",
        ),
        ("incomplete", "en", "Example solar plant: forecast for Mon 16 Nov is incomplete"),
        ("incomplete", "tr", "Example solar plant: 16 Kas\u0131m Pazartesi tahmini eksik"),
        ("missing", "en", "Example solar plant: no forecast for Mon 16 Nov"),
        (
            "missing",
            "tr",
            "Example solar plant: 16 Kas\u0131m Pazartesi tahmini haz\u0131rlanamad\u0131",
        ),
    ],
)
def test_the_subject_names_the_plant_first_and_tomorrows_state(
    kind: str, language: str, subject: str
) -> None:
    tomorrow = {"incomplete": payload(missing=True), "missing": empty()}.get(kind, payload())
    source = context(tomorrow)
    source["plant_name"] = "Example  solar\nplant"
    results: list[dict[str, object] | None] | None = None
    if kind == "irradiance":
        source["plant_id"] = None
        results = [payload(), payload()]
    message = rendered(source, language, results=results)
    assert message["subject"] == subject
    assert message["attachment_name"] == "pyracast-example-solar-plant-2026-11-16.xlsx"
    assert f'aria-label="{subject}"' in message["html"]


@pytest.mark.parametrize(
    ("language", "suffix"), [("en", " (14:00 edition)"), ("tr", " (14:00 gönderimi)")]
)
def test_a_second_clock_of_the_day_names_its_edition_in_subject_and_file(
    language: str, suffix: str
) -> None:
    single = rendered(context(payload()), language)
    morning = rendered(context(payload()), language, edition="08:00")
    afternoon = rendered(context(payload()), language, edition="14:00")
    assert afternoon["subject"] == single["subject"] + suffix
    assert morning["subject"] != afternoon["subject"]
    assert morning["attachment_name"] == "pyracast-example-solar-plant-2026-11-16-0800.xlsx"
    assert afternoon["attachment_name"] == "pyracast-example-solar-plant-2026-11-16-1400.xlsx"
    assert single["attachment_name"] == "pyracast-example-solar-plant-2026-11-16.xlsx"
    assert afternoon["body"] == single["body"]
    assert "14:00" not in afternoon["body"]
    missing = rendered(context(empty(), today=empty()), language, edition="14:00")
    assert missing["subject"].endswith(suffix)


@pytest.mark.parametrize(
    ("case", "language", "figure", "note"),
    [
        (
            "included",
            "en",
            "1.00",
            "Includes the recorded outage. Without it: 2.50\u00a0MWh.",
        ),
        (
            "included",
            "tr",
            "1,00",
            "Kay\u0131tl\u0131 kesinti dahil. Kesinti olmasayd\u0131: 2,50\u00a0MWh.",
        ),
        (
            "unknown",
            "en",
            "2.50",
            "The recorded outage is not included in this figure. Enter the capacity still in "
            "service in the outage record to include it.",
        ),
        (
            "unknown",
            "tr",
            "2,50",
            "Kay\u0131tl\u0131 kesinti bu rakama dahil değildir. Dahil edilmesi için "
            "kesinti kayd\u0131na devrede kalan gücü girin.",
        ),
        ("excluded", "en", "2.50", "The recorded outage is not included in this figure.</p>"),
        ("excluded", "tr", "2,50", "Kay\u0131tl\u0131 kesinti bu rakama dahil değildir.</p>"),
    ],
)
def test_a_recorded_outage_is_stated_under_the_figure(
    case: str, language: str, figure: str, note: str
) -> None:
    outage = payload(total=1.0) if case != "excluded" else payload(missing=True)
    source = context(
        payload(), outage, status="unknown_impact" if case == "unknown" else "estimated"
    )
    message = rendered(source, language)
    assert f">{figure}&nbsp;<span" in message["html"]
    assert note in message["html"]
    assert note.replace("\u00a0", " ").removesuffix("</p>") in message["body"]
    for removed in ("Normal operation", "With reported outage", "Normal çal\u0131şma", "scenario"):
        assert removed not in message["html"]


@pytest.mark.parametrize(
    ("language", "incomplete", "missing"),
    [
        (
            "en",
            "Data is missing for some hours, so no daily total is given.",
            "No forecast data for this day.",
        ),
        (
            "tr",
            "Baz\u0131 saatlerin verisi eksik olduğundan günlük toplam verilmedi.",
            "Bu gün için tahmin verisi yok.",
        ),
    ],
)
def test_a_day_without_a_total_shows_a_sentence_and_no_partial_sum(
    language: str, incomplete: str, missing: str
) -> None:
    message = rendered(context(payload(missing=True), today=empty()), language)
    for part in (message["html"], message["body"]):
        assert incomplete in part
        assert missing in part
        assert "2.50" not in part
        assert "2,50" not in part
        assert "coverage" not in part
        assert "Partial" not in part
        assert "Unavailable" not in part


@pytest.mark.parametrize(
    ("language", "sentence", "label"),
    [
        (
            "en",
            "The forecast for Monday 16 November 2026 could not be prepared. This email "
            "contains no figures.",
            "Open plant in Pyracast",
        ),
        (
            "tr",
            "16 Kas\u0131m 2026 Pazartesi için üretim tahmini haz\u0131rlanamad\u0131. "
            "Bu e-postada rakam yoktur.",
            "Santrali Pyracast&#x27;te aç",
        ),
    ],
)
def test_without_any_data_the_mail_has_no_figure_and_no_file(
    language: str, sentence: str, label: str
) -> None:
    source = context(empty(), today=empty())
    source["collected_at"] = None
    message = rendered(source, language)
    assert "attachment_base64" not in message
    assert "attachment_name" not in message
    assert sentence in message["html"]
    assert sentence in message["body"]
    assert f'<a href="{PLANT_URL}" ' in message["html"]
    assert f">{label}</a>" in message["html"]
    assert f"{PLANT_URL}\n" in message["body"]
    assert "MWh" not in message["html"] + message["body"]
    assert "Excel" not in message["html"] + message["body"]


@pytest.mark.parametrize(
    ("electrical", "address"),
    [
        (True, f"{PLANT_URL}?date=2026-11-16"),
        (False, f"{PLANT_URL}?date=2026-11-16&view=irradiance"),
    ],
)
def test_the_only_link_opens_the_plants_forecast_for_tomorrow(
    electrical: bool, address: str
) -> None:
    source = context(payload())
    results: list[dict[str, object] | None] | None = None
    if not electrical:
        source["plant_id"] = None
        results = [payload(), payload()]
    message = rendered(source, results=results)
    html = message["html"]
    assert html.count("href=") == 1
    assert f'href="{address.replace("&", "&amp;")}"' in html
    assert ">Open forecast in Pyracast</a>" in html
    assert "Or copy this link" not in html
    assert message["body"].count("http") == 1
    assert f"Open forecast in Pyracast:\n{address}\n" in message["body"]


@pytest.mark.parametrize(
    ("language", "kind", "attachment"),
    [
        ("en", "Solar irradiance forecast (GHI), not electricity generation", "Interval values"),
        (
            "tr",
            "Güneş \u0131ş\u0131n\u0131m\u0131 tahmini (GHI); elektrik üretimi değildir",
            "Aral\u0131k değerleri",
        ),
    ],
)
def test_an_irradiance_mail_says_what_it_measures_once(
    language: str, kind: str, attachment: str
) -> None:
    source = context(None)
    source["plant_id"] = None
    message = rendered(source, language, results=[payload(), payload()])
    assert message["html"].count(kind) == 1
    assert f"{attachment} " in message["html"]
    assert "kWh/m²" in message["body"]
    assert "electricity." not in message["html"]


def test_the_mail_follows_the_frame_and_stays_small() -> None:
    html = rendered(context(payload()))["html"]
    assert len(html.encode()) < 15_000
    for meta in (
        '<meta name="color-scheme" content="light dark">',
        '<meta name="supported-color-schemes" content="light dark">',
        '<meta name="format-detection" content="telephone=no,date=no,address=no,email=no">',
        '<meta name="x-apple-disable-message-reformatting">',
    ):
        assert meta in html
    assert '<div role="article" aria-roledescription="email"' in html
    assert 'lang="en" dir="ltr">' in html
    assert ">pyracast</td>" in html
    assert "@media (prefers-color-scheme: dark)" in html
    assert "<!--[if mso]>" in html
    assert "<img" not in html
    assert "src=" not in html
    assert "url(" not in html
    assert "text-transform" not in html
    assert "#475569" not in html
    assert ">Hourly values are in the attached Excel file.</p>" in html
    assert 'class="mail-button" bgcolor="#09383a" style="mso-padding-alt:12px 22px;' in html
    assert 'text-align:center;">\n    <a href="' in html
    assert 'style="display:block;padding:12px 22px;' in html


@pytest.mark.parametrize(
    ("offset", "text"),
    [
        (timedelta(hours=3), "UTC+3"),
        (timedelta(hours=5, minutes=30), "UTC+5:30"),
        (timedelta(hours=-4), "UTC-4"),
        (timedelta(0), "UTC+0"),
    ],
)
def test_the_offset_is_written_in_hours_and_only_needed_minutes(
    offset: timedelta, text: str
) -> None:
    assert utc_offset(datetime(2026, 11, 25, 16, 2, tzinfo=timezone(offset))) == text


@pytest.mark.parametrize(
    ("plant", "edition", "name"),
    [
        ("Karap\u0131nar GES", None, "pyracast-karapinar-ges-2026-11-26.xlsx"),
        ("İĞNE ŞÜÖÇ \u0131ğşüöç", "07:30", "pyracast-igne-suoc-igsuoc-2026-11-26-0730.xlsx"),
        ("  Plant / Ünit #2 — East  ", None, "pyracast-plant-unit-2-east-2026-11-26.xlsx"),
        ("太陽", None, "pyracast-plant-2026-11-26.xlsx"),
        ("\u212a", None, "pyracast-plant-2026-11-26.xlsx"),
        ("Stra\u00dfe \u212aelvin", None, "pyracast-stra-e-elvin-2026-11-26.xlsx"),
        ("a" * 39 + " b", None, f"pyracast-{'a' * 39}-2026-11-26.xlsx"),
    ],
)
def test_the_file_name_is_a_lower_case_ascii_slug_of_the_plant(
    plant: str, edition: str | None, name: str
) -> None:
    assert file_name(plant, date(2026, 11, 26), edition) == name


@pytest.mark.parametrize(
    ("value", "en", "tr"),
    [
        (3.2994, "3.30", "3,30"),
        (2500.0, "2,500.00", "2.500,00"),
        (-0.001, "0.00", "0,00"),
        (1e26, f"100{',000' * 8}.00", f"100{'.000' * 8},00"),
    ],
)
def test_energy_is_rounded_to_two_grouped_decimals(value: float, en: str, tr: str) -> None:
    for language, text in (("en", en), ("tr", tr)):
        with translation.override(language):
            assert energy_number(value) == text


def stored_cells(message: dict[str, str], references: tuple[str, ...]) -> list[str]:
    archive = ZipFile(BytesIO(b64decode(message["attachment_base64"])))
    sheet = archive.read("xl/worksheets/sheet1.xml").decode()
    cells = dict(re.findall(r'<c r="([A-Z]+[0-9]+)"[^>]*><v>([^<]*)</v>', sheet))
    return [cells[reference] for reference in references]


@pytest.mark.parametrize(
    ("language", "figure", "without", "preheader"),
    [
        ("en", "24.13", "30.13", "Tomorrow 24.13\u00a0MWh · Today 1.25\u00a0MWh"),
        ("tr", "24,13", "30,13", "Yar\u0131n 24,13\u00a0MWh · Bugün 1,25\u00a0MWh"),
    ],
)
@pytest.mark.parametrize("below", [False, True], ids=["half cent", "just below half cent"])
def test_a_half_cent_rounds_up_in_the_mail_as_the_file_displays_it(
    language: str, figure: str, without: str, preheader: str, below: bool
) -> None:
    tomorrow, outage = (nextafter(value, 0) if below else value for value in (24.125, 30.125))
    source = context(payload(total=outage), payload(total=tomorrow), status="estimated")
    message = rendered(source, language)
    assert stored_cells(message, ("C11", "D11")) == ["24.125", "30.125"]
    assert f">{figure}&nbsp;<span" in message["html"]
    assert f"{figure} MWh" in message["body"]
    assert f"{without}\u00a0MWh." in message["html"]
    assert f"{without} MWh." in message["body"]
    assert f">{preheader}&#847;" in message["html"]
    book = load_workbook(BytesIO(b64decode(message["attachment_base64"])))
    summary = book.worksheets[0]
    assert [summary.cell(11, column).value for column in (3, 4)] == [24.125, 30.125]
    assert {summary.cell(11, column).number_format for column in (3, 4)} == {"#,##0.00"}
