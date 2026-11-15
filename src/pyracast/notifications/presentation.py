from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import cast

from django.conf import settings
from django.utils.translation import gettext as _


@dataclass(frozen=True)
class ReportInterval:
    start: datetime
    end: datetime
    energy: float | None
    outage_energy: float | None
    complete: bool
    outage_complete: bool
    source_kind: str = "forecast"


@dataclass(frozen=True)
class ReportDay:
    day: date
    total: float | None
    partial: float
    coverage: float | None
    outage_total: float | None
    outage_partial: float
    outage_coverage: float | None
    has_outage: bool
    intervals: tuple[ReportInterval, ...]
    outage_status: str | None = None
    source_kind: str = "forecast"


@dataclass(frozen=True)
class ForecastReport:
    plant_name: str
    timezone: str
    collected_at: datetime | None
    prepared_at: datetime
    days: tuple[ReportDay, ...]
    unit: str
    electrical: bool
    warnings: tuple[str, ...]
    url: str


def report_summary(payload: dict[str, object] | None) -> dict[str, object]:
    if payload is None:
        return {}
    return cast(dict[str, object], payload.get("daylight", payload))


def _energy(row: dict[str, object], unit: str) -> float | None:
    value = row.get(f"energy_{unit}")
    if value is not None:
        return float(cast(float, value))
    if cast(float, row.get("coverage_fraction") or 0) > 0:
        return float(cast(float, row.get(f"partial_energy_{unit}", 0)))
    return None


def _rows(payload: dict[str, object] | None) -> list[dict[str, object]]:
    if payload is None:
        return []
    return cast(list[dict[str, object]], payload.get("report_intervals", payload["intervals"]))


def _visible(row: dict[str, object], payload: dict[str, object]) -> bool:
    if "report_intervals" in payload:
        return True
    daylight = cast(dict[str, object] | None, payload.get("daylight"))
    if daylight is None:
        return True
    begin = datetime.fromisoformat(cast(str, row["start"]))
    end = datetime.fromisoformat(cast(str, row["end"]))
    return any(
        begin < datetime.fromisoformat(window["end"])
        and end > datetime.fromisoformat(window["start"])
        for window in cast(list[dict[str, str]], daylight["windows"])
    )


def _intervals(
    normal: dict[str, object] | None, outage: dict[str, object] | None, *, electrical: bool
) -> tuple[ReportInterval, ...]:
    if normal is None:
        return ()
    outage_rows = {cast(str, row["start"]): row for row in _rows(outage)}
    rows = []
    for row in _rows(normal):
        begin = datetime.fromisoformat(cast(str, row["start"]))
        end = datetime.fromisoformat(cast(str, row["end"]))
        irradiance = cast(float | None, row.get("ghi_wm2"))
        energy = (
            _energy(row, "mwh")
            if electrical
            else None
            if irradiance is None
            else irradiance * (end - begin).total_seconds() / 3_600_000
        )
        if not _visible(row, normal):
            continue
        other = outage_rows.get(cast(str, row["start"]), {})
        rows.append(
            ReportInterval(
                start=begin,
                end=end,
                energy=energy,
                outage_energy=_energy(other, "mwh"),
                complete=row.get("complete") is True if electrical else energy is not None,
                outage_complete=other.get("complete") is True,
                source_kind=_interval_source(row, normal, begin, end),
            )
        )
    return tuple(rows)


def _interval_source(
    row: dict[str, object], normal: dict[str, object], begin: datetime, end: datetime
) -> str:
    provenance = cast(dict[str, object], normal.get("provenance", {}))
    kinds = set(cast(list[str], row.get("source_kinds", [])))
    if not kinds:
        kinds = {
            cast(str, segment.get("source_kind", "forecast"))
            for segment in cast(list[dict[str, object]], provenance.get("source_segments", []))
            if begin < datetime.fromisoformat(cast(str, segment["end"]))
            and end > datetime.fromisoformat(cast(str, segment["start"]))
        }
    if len(kinds) > 1:
        return "mixed"
    return next(iter(kinds), cast(str, provenance.get("source_kind", "forecast")))


def source_label(source_kind: str) -> str:
    return {
        "forecast": _("Forecast"),
        "estimated_actuals": _("Estimated actuals"),
        "mixed": _("Estimated + forecast"),
    }[source_kind]


def _day(
    day: date,
    normal: dict[str, object] | None,
    outage: dict[str, object] | None,
    *,
    electrical: bool,
    outage_status: str | None,
) -> ReportDay:
    unit = "mwh" if electrical else "kwh_m2"
    summary, alternative = report_summary(normal), report_summary(outage)
    provenance = cast(dict[str, object], normal.get("provenance", {})) if normal else {}
    return ReportDay(
        day=day,
        total=cast(float | None, summary.get(f"energy_{unit}")),
        partial=cast(float, summary.get(f"partial_energy_{unit}", 0)),
        coverage=cast(float | None, summary.get("coverage_fraction")),
        outage_total=cast(float | None, alternative.get(f"energy_{unit}")),
        outage_partial=cast(float, alternative.get(f"partial_energy_{unit}", 0)),
        outage_coverage=cast(float | None, alternative.get("coverage_fraction")),
        has_outage=outage is not None,
        intervals=_intervals(normal, outage, electrical=electrical),
        outage_status=outage_status,
        source_kind=cast(str, provenance.get("source_kind", "forecast")),
    )


def present_report(
    context: dict[str, object],
    results: list[dict[str, object] | None],
    *,
    prepared_at: datetime,
    fallback: bool,
) -> ForecastReport:
    electrical = context["plant_id"] is not None
    days = []
    collected = context["collected_at"]
    source_times = [datetime.fromisoformat(cast(str, collected))] if collected else []
    for row, irradiance in zip(
        cast(list[dict[str, object]], context["days"]), results, strict=True
    ):
        normal = cast(dict[str, object] | None, row["normal"]) if electrical else irradiance
        provenance = cast(dict[str, object], normal.get("provenance", {})) if normal else {}
        for field in ("collected_at", "oldest_collected_at"):
            if timestamp := provenance.get(field):
                source_times.append(datetime.fromisoformat(cast(str, timestamp)))
        source_times.extend(
            datetime.fromisoformat(cast(str, source["collected_at"]))
            for source in cast(list[dict[str, object]], provenance.get("source_snapshots", []))
        )
        days.append(
            _day(
                date.fromisoformat(cast(str, row["date"])),
                normal,
                cast(dict[str, object] | None, row["outage"]) if electrical else None,
                electrical=electrical,
                outage_status=cast(str | None, row.get("outage_status")),
            )
        )
    collected_at = max(source_times) if source_times else None
    warnings = []
    if not electrical:
        warnings.append(_("Irradiance estimate, not plant electricity."))
    if source_times and prepared_at - min(source_times) > timedelta(hours=24):
        warnings.append(_("Forecast data is over 24 hours old."))
    if collected_at and fallback:
        warnings.append(_("Using the last saved forecast."))
    if any(day.has_outage for day in days):
        warnings.append(_("The outage scenario follows the recorded outage times and limits."))
    if any(day.outage_status == "unknown_impact" for day in days):
        warnings.append(
            _("Outage impact is unknown. Update the outage capacity to get an estimate.")
        )
    return ForecastReport(
        plant_name=cast(str, context["plant_name"]),
        timezone=cast(str, context["timezone"]),
        collected_at=collected_at,
        prepared_at=prepared_at,
        days=tuple(days),
        unit="MWh" if electrical else "kWh/m²",
        electrical=electrical,
        warnings=tuple(warnings),
        url=f"{settings.PYRACAST_PUBLIC_ORIGIN}/app/",
    )
