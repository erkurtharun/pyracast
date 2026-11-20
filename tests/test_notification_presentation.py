from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest
from django.utils import translation

from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.presentation import present_report
from pyracast.notifications.rendering import energy_text, render_forecast

START = datetime(2026, 11, 15, 8, tzinfo=UTC)


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


def payload(*, missing: bool = False) -> dict[str, object]:
    rows = [interval(0, 2.5), interval(1, None if missing else 0, 0 if missing else 1)]
    return {
        "intervals": rows,
        "daylight": {
            "energy_mwh": None if missing else 2.5,
            "partial_energy_mwh": 2.5,
            "energy_kwh_m2": None if missing else 2.5,
            "partial_energy_kwh_m2": 2.5,
            "coverage_fraction": 0.5 if missing else 1.0,
            "windows": [
                {"start": START.isoformat(), "end": (START + timedelta(hours=2)).isoformat()}
            ],
        },
    }


def context(
    normal: dict[str, object] | None, outage: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "plant_id": 1,
        "plant_name": "Example solar plant",
        "timezone": "Europe/Istanbul",
        "collected_at": START.isoformat(),
        "days": [{"date": "2026-11-15", "normal": normal, "outage": outage}],
    }


def test_electricity_report_distinguishes_known_zero_and_missing_outage() -> None:
    normal, outage = payload(), payload(missing=True)
    normal["report_intervals"] = normal["intervals"]
    report = present_report(context(normal, outage), [None], prepared_at=START, fallback=False)
    day = report.days[0]
    assert report.unit == "MWh"
    assert report.url == "https://testserver/app/"
    assert day.day == date(2026, 11, 15)
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
    assert len(report.warnings) == 1


def test_unavailable_electricity_is_not_substituted_with_irradiance() -> None:
    source = context(None)
    source["collected_at"] = None
    report = present_report(source, [payload()], prepared_at=START, fallback=False)
    assert report.electrical
    assert report.collected_at is None
    assert report.days[0].total is None
    assert report.days[0].coverage is None
    assert report.days[0].intervals == ()
    assert not report.warnings


def test_regional_irradiance_uses_native_daylight_rows_and_explicit_units() -> None:
    source = context(None)
    source["plant_id"] = None
    data = payload()
    data["intervals"] = [
        interval(-1, 9),
        *cast(list[dict[str, object]], data["intervals"]),
        interval(2, 9),
    ]
    report = present_report(source, [data], prepared_at=START + timedelta(days=2), fallback=True)
    day = report.days[0]
    assert report.unit == "kWh/m²"
    assert not report.electrical
    assert [item.energy for item in day.intervals] == [2.5, 0]
    assert sum(item.energy or 0 for item in day.intervals) == day.total
    assert len(report.warnings) == 3
    assert "24 hours" in report.warnings[1]
    assert "last saved forecast" in report.warnings[2]


def test_missing_irradiance_is_a_gap_and_legacy_unknown_daylight_is_preserved() -> None:
    source = context(None)
    source["plant_id"] = None
    report = present_report(source, [payload(missing=True)], prepared_at=START, fallback=False)
    assert report.days[0].intervals[1].energy is None
    assert not report.days[0].intervals[1].complete
    old = {"intervals": [interval(0, 2.5)], "energy_kwh_m2": 2.5}
    report = present_report(source, [old], prepared_at=START, fallback=False)
    assert report.days[0].total == 2.5
    assert len(report.days[0].intervals) == 1


def test_partial_interval_preserves_covered_energy_without_claiming_completeness() -> None:
    data = payload(missing=True)
    row = cast(list[dict[str, object]], data["intervals"])[1]
    row.update(partial_energy_mwh=1.25, coverage_fraction=0.5)
    report = present_report(context(data), [None], prepared_at=START, fallback=False)
    assert report.days[0].intervals[1].energy == 1.25
    assert not report.days[0].intervals[1].complete


@pytest.mark.parametrize("language", ["en", "tr"])
def test_energy_label_never_presents_missing_as_zero_or_partial_as_daily_total(
    language: str,
) -> None:
    with translation.override(language):
        assert energy_text(None, 0, None, "MWh") == (
            "Unavailable" if language == "en" else "Veri yok"
        )
        assert energy_text(0, 0, 1, "MWh") == ("0.000 MWh" if language == "en" else "0,000 MWh")
        text = energy_text(None, 1.25, 0.5, "MWh")
        assert "Partial" in text if language == "en" else "K\u0131smi" in text
        assert "50%" in text


def test_unknown_outage_remains_distinct_from_partial_source_data() -> None:
    source = context(payload(), payload(missing=True))
    cast(list[dict[str, object]], source["days"])[0]["outage_status"] = "unknown_impact"
    report = present_report(source, [None], prepared_at=START, fallback=False)
    assert report.days[0].outage_status == "unknown_impact"
    assert any("Outage impact is unknown" in warning for warning in report.warnings)


@pytest.mark.parametrize("direct_sources", [False, True])
def test_mixed_report_keeps_each_interval_source_including_missing_and_zero(
    direct_sources: bool,
) -> None:
    data = payload(missing=True)
    rows = cast(list[dict[str, object]], data["intervals"])
    rows.append(interval(2, 0))
    cast(dict[str, object], data["daylight"])["windows"] = [
        {"start": START.isoformat(), "end": (START + timedelta(hours=3)).isoformat()}
    ]
    cast(dict[str, object], data["daylight"])["coverage_fraction"] = 2 / 3
    kinds = [["estimated_actuals"], ["estimated_actuals", "forecast"], ["forecast"]]
    data["provenance"] = {
        "source_kind": "mixed",
        "source_segments": [
            {
                "start": START.isoformat(),
                "end": (START + timedelta(minutes=90)).isoformat(),
                "source_kind": "estimated_actuals",
            },
            {
                "start": (START + timedelta(minutes=90)).isoformat(),
                "end": (START + timedelta(hours=3)).isoformat(),
                "source_kind": "forecast",
            },
        ],
    }
    if direct_sources:
        for row, sources in zip(rows, kinds, strict=True):
            row["source_kinds"] = sources
        cast(dict[str, object], data["provenance"])["source_segments"] = []
    report = present_report(context(data), [None], prepared_at=START, fallback=False)
    day = report.days[0]
    assert day.source_kind == "mixed"
    assert [row.source_kind for row in day.intervals] == ["estimated_actuals", "mixed", "forecast"]
    assert [row.energy for row in day.intervals] == [2.5, None, 0]
    assert [row.complete for row in day.intervals] == [True, False, True]
    assert day.total is None
    assert day.coverage == pytest.approx(2 / 3)


def test_regional_mixed_report_shows_latest_update_and_warns_about_old_contributor() -> None:
    source = context(None)
    source["plant_id"] = None
    source["collected_at"] = None
    data = payload()
    latest = START - timedelta(minutes=5)
    oldest = START - timedelta(hours=25)
    data["provenance"] = {
        "source_kind": "mixed",
        "source_snapshots": [
            {
                "snapshot_id": 2,
                "source_kind": "estimated_actuals",
                "collected_at": latest.isoformat(),
            },
            {"snapshot_id": 1, "source_kind": "forecast", "collected_at": oldest.isoformat()},
        ],
    }
    report = present_report(source, [data], prepared_at=START, fallback=False)
    assert report.collected_at == latest
    assert report.days[0].source_kind == "mixed"
    assert "Forecast data is over 24 hours old." in report.warnings
    assert "Irradiance estimate, not plant electricity." in report.warnings


@pytest.mark.parametrize("source_kind", ["estimated_actuals", "mixed"])
@pytest.mark.parametrize("language", ["en", "tr"])
def test_email_labels_actuals_and_mixed_partial_without_hiding_unknown_outage(
    source_kind: str,
    language: str,
) -> None:
    normal = payload(missing=source_kind == "mixed")
    normal["provenance"] = {"source_kind": source_kind}
    outage = payload(missing=True) if source_kind == "mixed" else None
    source = context(normal, outage)
    if outage is not None:
        cast(dict[str, object], outage["daylight"]).update(
            partial_energy_mwh=0, coverage_fraction=0
        )
        cast(list[dict[str, object]], source["days"])[0]["outage_status"] = "unknown_impact"
    with translation.override(language):
        rendered = render_forecast(
            NotificationDelivery(scheduled_for=START),
            [None],
            prepared_at=START,
            report_context=source,
            footer="",
        )
    labels = {
        "en": {"estimated_actuals": "Estimated actuals", "mixed": "Estimated + forecast"},
        "tr": {"estimated_actuals": "Gerçekleşen tahmini", "mixed": "Gerçekleşen tahmini + öngörü"},
    }
    label = labels[language][source_kind]
    for field in ("body", "html"):
        assert label in rendered[field]
        if source_kind == "mixed":
            assert (
                "Partial: 2.500 MWh (50% coverage)"
                if language == "en"
                else "K\u0131smi: 2,500 MWh (50% kapsam)"
            ) in rendered[field]
            assert (
                "Outage impact is unknown" if language == "en" else "Kesintinin etkisi belirsiz"
            ) in rendered[field]
            assert ("Unavailable" if language == "en" else "Veri yok") in rendered[field]
        else:
            assert ("2.500 MWh" if language == "en" else "2,500 MWh") in rendered[field]
