from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.utils import timezone

from pyracast.accounts.authorization import tenant_membership
from pyracast.db import read_snapshot
from pyracast.errors import DomainError, ErrorCode
from pyracast.forecasts.faults import FROZEN_PAYLOAD_FAULTS, stored_data
from pyracast.plants.history import automatic_outlook
from pyracast.plants.models import Plant
from pyracast.plants.production import NativeSlot, native_production_days
from pyracast.regions.models import Region

FORECAST_DAYS_BEFORE = 7
FORECAST_DAYS_AFTER = 14
PRODUCTION_DAYS_BEFORE = 30
RESOLUTION_MINUTES = 60


@dataclass(frozen=True)
class ForecastRow:
    start: datetime
    end: datetime
    mwh: float | None
    affected_mwh: float | None
    sources: tuple[str, ...] | None


@dataclass(frozen=True)
class Affected:
    status: Literal["estimated", "unknown_impact"]
    total_mwh: float | None
    partial: bool


@dataclass(frozen=True)
class ForecastDay:
    date: date
    timezone: str
    source_kind: str
    freshness: str
    stale_reason: str | None
    unavailable_reason: str | None
    collected_at: datetime | None
    total_mwh: float | None
    partial: bool
    affected: Affected | None
    rows: tuple[ForecastRow, ...]


@dataclass(frozen=True)
class ProductionRow:
    start: datetime
    end: datetime
    mwh: float | None
    quality: str | None
    source: str | None
    provisional: bool | None


@dataclass(frozen=True)
class ProductionDay:
    date: date
    timezone: str
    total_mwh: float | None
    complete: bool
    rows: tuple[ProductionRow, ...]


@dataclass(frozen=True)
class PlantDays:
    plant_id: int
    name: str
    today: date
    forecast: tuple[ForecastDay, ...]
    production: tuple[ProductionDay, ...]


def plant_days(actor: User, tenant_id: int, region_id: int, plant_id: int) -> PlantDays:
    with read_snapshot():
        tenant_membership(actor, tenant_id)
        region = Region.objects.active().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.get(pk=plant_id, region=region)
        plant.region = region
        now = timezone.now()
        today = now.astimezone(ZoneInfo(region.timezone)).date()
        return PlantDays(
            plant_id=plant.pk,
            name=plant.name,
            today=today,
            forecast=tuple(
                _forecast_day(plant, today + timedelta(days=offset), today, now)
                for offset in range(-FORECAST_DAYS_BEFORE, FORECAST_DAYS_AFTER + 1)
            ),
            production=_production_days(plant, today, now),
        )


def _forecast_day(plant: Plant, day: date, today: date, now: datetime) -> ForecastDay:
    try:
        with stored_data("plant", plant.pk, day, "days", FROZEN_PAYLOAD_FAULTS):
            return _projected(
                day, cast(dict[str, Any], automatic_outlook(plant, day, RESOLUTION_MINUTES, now))
            )
    except DomainError as refusal:
        if refusal.code != ErrorCode.CALCULATION_OUT_OF_RANGE:
            raise
        return _refused_day(plant, day, today)


def _projected(day: date, outlook: dict[str, Any]) -> ForecastDay:
    result: dict[str, Any] | None = outlook["result"]
    scenario: dict[str, Any] | None = outlook["outage_scenario"]
    total, partial = _energy(result)
    return ForecastDay(
        date=day,
        timezone=outlook["timezone"],
        source_kind=outlook["source_kind"],
        freshness=outlook["freshness"],
        stale_reason=outlook["stale_reason"],
        unavailable_reason=outlook["unavailable_reason"],
        collected_at=_collected_at(result),
        total_mwh=total,
        partial=partial,
        affected=_affected(scenario),
        rows=_forecast_rows(result, scenario, mixed=outlook["source_kind"] == "mixed"),
    )


def _refused_day(plant: Plant, day: date, today: date) -> ForecastDay:
    return ForecastDay(
        date=day,
        timezone=plant.region.timezone,
        source_kind="estimated_actuals" if day < today else "forecast",
        freshness="missing",
        stale_reason=None,
        unavailable_reason=ErrorCode.CALCULATION_OUT_OF_RANGE,
        collected_at=None,
        total_mwh=None,
        partial=False,
        affected=None,
        rows=(),
    )


def _energy(result: dict[str, Any] | None) -> tuple[float | None, bool]:
    if result is None:
        return None, False
    daylight: dict[str, Any] | None = result.get("daylight")
    summary = daylight if daylight is not None else result
    no_daylight = daylight is not None and daylight.get("status") == "polar_night"
    partial = summary.get("complete") is not True and not no_daylight
    if partial and summary.get("coverage_fraction") == 0:
        return None, partial
    return _figure(summary.get("partial_energy_mwh" if partial else "energy_mwh")), partial


def _figure(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(type(value).__name__)
    figure = float(value)
    if not isfinite(figure):
        raise ValueError(figure)
    return figure


def _instant(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _collected_at(result: dict[str, Any] | None) -> datetime | None:
    stamp = (result or {}).get("provenance", {}).get("collected_at")
    return _instant(stamp) if stamp is not None else None


def _affected(scenario: dict[str, Any] | None) -> Affected | None:
    if scenario is None:
        return None
    total, partial = _energy(scenario["result"])
    return Affected(status=scenario["status"], total_mwh=total, partial=partial)


def _forecast_rows(
    result: dict[str, Any] | None, scenario: dict[str, Any] | None, *, mixed: bool
) -> tuple[ForecastRow, ...]:
    if result is None:
        return ()
    affected = (
        {row["start"]: _figure(row.get("energy_mwh")) for row in scenario["result"]["intervals"]}
        if scenario is not None
        else {}
    )
    rows = tuple(
        ForecastRow(
            start=_instant(row["start"]),
            end=_instant(row["end"]),
            mwh=_figure(row.get("energy_mwh")),
            affected_mwh=affected.get(row["start"]),
            sources=_sources(row) if mixed else None,
        )
        for row in result["intervals"]
    )
    return rows if any(row.mwh is not None or row.affected_mwh is not None for row in rows) else ()


def _sources(row: dict[str, Any]) -> tuple[str, ...] | None:
    kinds: list[str] | None = row.get("source_kinds")
    return tuple(kinds) if kinds is not None else None


def _production_days(plant: Plant, today: date, now: datetime) -> tuple[ProductionDay, ...]:
    production = native_production_days(
        plant, today - timedelta(days=PRODUCTION_DAYS_BEFORE), today - timedelta(days=1), now
    )
    return tuple(
        ProductionDay(
            date=day.date,
            timezone=production.timezone,
            total_mwh=day.total_mwh,
            complete=day.complete,
            rows=_with_figures(tuple(_production_row(slot) for slot in day.slots)),
        )
        for day in production.days
    )


def _production_row(slot: NativeSlot) -> ProductionRow:
    value = slot.effective
    return ProductionRow(
        start=slot.starts_at,
        end=slot.ends_at,
        mwh=value["energy_mwh"] if value else None,
        quality=value["quality"] if value else None,
        source=value["source"] if value else None,
        provisional=value["provisional"] if value else None,
    )


def _with_figures(rows: tuple[ProductionRow, ...]) -> tuple[ProductionRow, ...]:
    return rows if any(row.mwh is not None for row in rows) else ()
