from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, cast

# A collection runs at least daily; two hours cover one still retrying and the 25-hour day.
STALE_AFTER = timedelta(hours=26)


@dataclass(frozen=True)
class ReportInterval:
    start: datetime
    end: datetime
    energy: float | None
    outage_energy: float | None
    complete: bool
    outage_complete: bool


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


@dataclass(frozen=True)
class ForecastReport:
    plant_name: str
    timezone: str
    collected_at: datetime | None
    prepared_at: datetime
    days: tuple[ReportDay, ...]
    unit: str
    electrical: bool
    stale: bool


@dataclass(frozen=True)
class Figure:
    value: float | None
    without_outage: float | None
    gap: Literal["", "incomplete", "missing"]
    outage: Literal["", "included", "excluded", "unknown"]


def figure(day: ReportDay) -> Figure:
    if day.total is None:
        return Figure(None, None, "incomplete" if day.coverage else "missing", "")
    if not day.has_outage:
        return Figure(day.total, None, "", "")
    if day.outage_status == "unknown_impact":
        return Figure(day.total, None, "", "unknown")
    if day.outage_total is None:
        return Figure(day.total, None, "", "excluded")
    return Figure(day.outage_total, day.total, "", "included")


def interval_value(item: ReportInterval, shown: Figure) -> float | None:
    if shown.outage == "included":
        return item.outage_energy if item.outage_complete else None
    return item.energy if item.complete else None


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
            )
        )
    return tuple(rows)


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
    )


def present_report(
    context: dict[str, object],
    results: list[dict[str, object] | None],
    *,
    prepared_at: datetime,
    missed_collection: bool,
) -> ForecastReport:
    """The shown time is tomorrow's forecast collection; today's composed sources never enter it."""
    electrical = context["plant_id"] is not None
    days = [
        _day(
            date.fromisoformat(cast(str, row["date"])),
            cast(dict[str, object] | None, row["normal"]) if electrical else irradiance,
            cast(dict[str, object] | None, row["outage"]) if electrical else None,
            electrical=electrical,
            outage_status=cast(str | None, row.get("outage_status")),
        )
        for row, irradiance in zip(
            cast(list[dict[str, object]], context["days"]), results, strict=True
        )
    ]
    collected = context["collected_at"]
    collected_at = datetime.fromisoformat(cast(str, collected)) if collected else None
    return ForecastReport(
        plant_name=cast(str, context["plant_name"]),
        timezone=cast(str, context["timezone"]),
        collected_at=collected_at,
        prepared_at=prepared_at,
        days=tuple(days),
        unit="MWh" if electrical else "kWh/m²",
        electrical=electrical,
        stale=collected_at is not None
        and (missed_collection or prepared_at - collected_at > STALE_AFTER),
    )
