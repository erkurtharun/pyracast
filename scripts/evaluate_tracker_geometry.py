"""Compare the tracker geometry versions on an archive database, read-only.

Prints aggregates per plant ordinal only; the rules are in docs/IRRADIANCE.md, "Tracking
geometry transfer".
"""

import os
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from math import isfinite, sqrt
from statistics import NormalDist, fmean, median
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from pyracast.plants import tracking
from pyracast.plants.numerics import CalibrationSample, PowerInterval, fit_calibration
from pyracast.solar import daylight_window

VERSIONS = (tracking.MODEL_V1, tracking.MODEL_V2)
LABELS = {tracking.MODEL_V1: "v1", tracking.MODEL_V2: "v2"}
EPIAS_ZONE = ZoneInfo("Europe/Istanbul")
EPIAS_POLICY = "hour-start-istanbul-v1"
SOURCE_PRIORITY = ("settlement_injection", "realtime_generation")
HOUR = timedelta(hours=1)
WEEK_DAYS = 7
# fit_calibration holds out the last 7 complete days of its window and trains on the 30 before.
FIT_WINDOW_DAYS = 37
MIN_SCORED_WEEKS = 4
CONFIDENCE = 0.95
RULE = (
    "rule: v2 not worse unless the 95 % interval of RMSE(v2) - RMSE(v1), jackknifed over "
    f"scored weeks, lies wholly above zero; at least {MIN_SCORED_WEEKS} scored weeks"
)

CONFIGURATIONS = """
SELECT c.plant_id, c.id, c.dc_capacity_mw, c.inverter_capacity_mw, c.export_limit_mw,
       c.measurement_basis, c.binding_id, b.capacity_kw, b.dc_capacity_kw, b.losses_percent,
       b.latitude, b.longitude, b.timezone
  FROM plants_plantconfiguration AS c
  JOIN plants_plant AS p ON p.id = c.plant_id
  JOIN connections_sitebinding AS b ON b.id = c.binding_id
 WHERE c.mounting_type = 'tracking'
 ORDER BY c.plant_id, c.revision, c.id
"""
PRODUCTION = """
SELECT DISTINCT ON (s.target_day, s.source_kind)
       s.target_day, s.source_kind, s.normalization_policy, s.readings
  FROM metering_epiassnapshot AS s
  JOIN metering_epiasconnection AS e ON e.id = s.connection_id
 WHERE s.configuration_id = %s AND e.plant_id = %s AND e.enabled
   AND s.provider_plant -> 'eic' = to_jsonb(e.eic)
   AND ((s.source_kind = 'realtime_generation'
         AND s.provider_plant -> 'id' = to_jsonb(e.provider_plant_id))
     OR (s.source_kind = 'settlement_injection'
         AND s.provider_plant -> 'id' = to_jsonb(e.settlement_plant_id)))
 ORDER BY s.target_day, s.source_kind, s.collected_at DESC, s.available_at DESC, s.id DESC
"""
MANUAL = """
SELECT starts_at, ends_at FROM plants_productionobservation
 WHERE configuration_id = %s AND is_current AND NOT is_reverted
"""
REFERENCES = """
SELECT normalized_intervals FROM forecasts_forecastsnapshot
 WHERE binding_id = %s AND source_kind = 'estimated_actuals'
 ORDER BY collected_at, id
"""

type Connection = psycopg.Connection[tuple[Any, ...]]
type Native = tuple[datetime, datetime, float]
type Windows = tuple[tuple[datetime, datetime], ...]


class MissingSettingError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class ReadOnlyRefusedError(Exception):
    pass


@dataclass(frozen=True)
class Configuration:
    id: int
    plant_id: int
    dc_capacity_mw: float
    limit_mw: float | None
    net_export: bool
    binding_id: int
    capacity_kw: float
    dc_capacity_kw: float | None
    losses_percent: float
    latitude: float
    longitude: float
    timezone: str

    @property
    def modeled(self) -> bool:
        return (
            self.dc_capacity_kw is not None
            and self.dc_capacity_kw > 0
            and 0 <= self.losses_percent < 100
        )


@dataclass
class Errors:
    count: int = 0
    absolute: float = 0.0
    squared: float = 0.0
    signed: float = 0.0
    share_absolute: float = 0.0
    share_squared: float = 0.0
    share_signed: float = 0.0

    def add(self, error_mwh: float, capacity_mwh: float) -> None:
        share = error_mwh / capacity_mwh
        self.count += 1
        self.absolute += abs(error_mwh)
        self.squared += error_mwh * error_mwh
        self.signed += error_mwh
        self.share_absolute += abs(share)
        self.share_squared += share * share
        self.share_signed += share


@dataclass(frozen=True)
class WeekErrors:
    squared_v1: float
    squared_v2: float
    count: int


@dataclass
class PlantReport:
    ordinal: int
    status: str = "evaluated"
    complete_days: int = 0
    evaluated_days: int = 0
    skipped_weeks: int = 0
    weeks: list[WeekErrors] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    errors: dict[str, Errors] = field(default_factory=lambda: {v: Errors() for v in VERSIONS})
    factors: dict[str, list[float]] = field(default_factory=lambda: {v: [] for v in VERSIONS})

    @property
    def difference(self) -> tuple[float, float, float] | None:
        return rmse_difference(self.weeks) if len(self.weeks) >= MIN_SCORED_WEEKS else None

    @property
    def not_worse(self) -> bool:
        difference = self.difference
        return difference is not None and difference[1] <= 0


@dataclass(frozen=True)
class _Hour:
    start: datetime
    energy_mwh: float
    pieces: dict[str, tuple[PowerInterval, ...]]


def connect(environ: Mapping[str, str]) -> Connection:
    for name in ("PYRACAST_ARCHIVE_USER", "PYRACAST_ARCHIVE_DATABASE"):
        if not environ.get(name):
            raise MissingSettingError(name)
    connection = psycopg.connect(
        host=environ.get("PYRACAST_ARCHIVE_HOST") or "127.0.0.1",
        port=environ.get("PYRACAST_ARCHIVE_PORT") or "5432",
        user=environ["PYRACAST_ARCHIVE_USER"],
        password=environ.get("PYRACAST_ARCHIVE_PASSWORD") or None,
        dbname=environ["PYRACAST_ARCHIVE_DATABASE"],
        options="-c default_transaction_read_only=on",
        application_name="pyracast-tracker-evaluation",
        connect_timeout=10,
    )
    connection.read_only = True
    connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    if connection.execute("SHOW default_transaction_read_only").fetchone() != ("on",):
        connection.close()
        raise ReadOnlyRefusedError
    return connection


def _configurations(connection: Connection) -> list[list[Configuration]]:
    plants: dict[int, list[Configuration]] = {}
    for row in connection.execute(CONFIGURATIONS):
        inverter, export, basis = row[3], row[4], row[5]
        limits = [
            value
            for value in (inverter, export if basis == "net_export" else None)
            if value is not None
        ]
        plants.setdefault(row[0], []).append(
            Configuration(
                id=row[1],
                plant_id=row[0],
                dc_capacity_mw=row[2],
                limit_mw=min(limits) if limits else None,
                net_export=basis == "net_export",
                binding_id=row[6],
                capacity_kw=row[7],
                dc_capacity_kw=row[8],
                losses_percent=row[9],
                latitude=row[10],
                longitude=row[11],
                timezone=row[12],
            )
        )
    return list(plants.values())


def _reading(row: object, target_day: date) -> tuple[datetime, float] | str:
    """One EPIAS reading as the measurement selection read it before 2026-11-18."""
    if not isinstance(row, dict):
        raise TypeError
    total = row.get("total_mwh")
    if total is None or row.get("reported") is False:
        return "unreported reading"
    begin = datetime.fromisoformat(row["period_start"])
    finish = datetime.fromisoformat(row["period_end"])
    energy = float(total)
    if isinstance(total, bool) or begin.utcoffset() is None or finish.utcoffset() is None:
        return "invalid reading"
    local = begin.astimezone(EPIAS_ZONE)
    if (
        finish - begin != HOUR
        or local.date() != target_day
        or (local.minute, local.second, local.microsecond) != (0, 0, 0)
        or not isfinite(energy)
        or energy < 0
    ):
        return "invalid reading"
    return begin.astimezone(UTC), energy


def _production(
    connection: Connection, configuration: Configuration, skipped: Counter[str]
) -> dict[datetime, float]:
    rows = connection.execute(PRODUCTION, (configuration.id, configuration.plant_id)).fetchall()
    hours: dict[datetime, float] = {}
    for target_day, _, policy, readings in sorted(
        rows, key=lambda row: SOURCE_PRIORITY.index(row[1])
    ):
        if policy != EPIAS_POLICY:
            skipped["unsupported normalization policy"] += 1
        elif not configuration.net_export:
            skipped["measurement basis not net export"] += 1
        elif not isinstance(readings, list):
            skipped["readings: TypeError"] += 1
        else:
            for row in readings:
                try:
                    parsed = _reading(row, target_day)
                except Exception as error:
                    skipped[f"reading: {type(error).__name__}"] += 1
                    continue
                if isinstance(parsed, str):
                    skipped[parsed] += 1
                else:
                    hours.setdefault(*parsed)
    for starts_at, ends_at in connection.execute(MANUAL, (configuration.id,)):
        for hour in [hour for hour in hours if hour < ends_at and hour + HOUR > starts_at]:
            del hours[hour]
            skipped["manual override"] += 1
    return hours


def _native(row: object) -> tuple[datetime, datetime, float | None] | None:
    if not isinstance(row, dict):
        raise TypeError
    start = datetime.fromisoformat(row["start"])
    end = datetime.fromisoformat(row["end"])
    power = row.get("power_kw")
    if (
        start.utcoffset() is None
        or end.utcoffset() is None
        or end <= start
        or (
            power is not None
            and (
                isinstance(power, bool)
                or not isinstance(power, int | float)
                or not isfinite(power)
                or power < 0
            )
        )
    ):
        return None
    return start.astimezone(UTC), end.astimezone(UTC), None if power is None else float(power)


def _complete(pieces: list[tuple[datetime, datetime, float | None]], hour: datetime) -> bool:
    cursor: datetime | None = None
    for start, end, power in sorted(pieces):
        if power is None or (start > hour if cursor is None else start != cursor):
            return False
        cursor = end
    return cursor is not None and cursor >= hour + HOUR


def _references(
    connection: Connection,
    configuration: Configuration,
    needed: set[datetime],
    skipped: Counter[str],
) -> dict[datetime, tuple[Native, ...]]:
    """Per measured hour, the native intervals of the latest snapshot that covers it whole."""
    covered: dict[datetime, tuple[Native, ...]] = {}
    if not needed:
        return covered
    with connection.cursor(name="tracker_references") as cursor:
        cursor.itersize = 20
        cursor.execute(REFERENCES, (configuration.binding_id,))
        for (intervals,) in cursor:
            if not isinstance(intervals, list):
                skipped["reference intervals: TypeError"] += 1
                continue
            by_hour: dict[datetime, list[tuple[datetime, datetime, float | None]]] = defaultdict(
                list
            )
            for row in intervals:
                try:
                    native = _native(row)
                except Exception as error:
                    skipped[f"reference interval: {type(error).__name__}"] += 1
                    continue
                if native is None:
                    skipped["invalid reference interval"] += 1
                    continue
                hour = native[0].replace(minute=0, second=0, microsecond=0)
                while hour < native[1]:
                    if hour in needed:
                        by_hour[hour].append(native)
                    hour += HOUR
            for hour, pieces in by_hour.items():
                if _complete(pieces, hour):
                    covered[hour] = tuple(
                        (start, end, power) for start, end, power in pieces if power is not None
                    )
    return covered


def transferred_mw(configuration: Configuration, native: Native, version: str) -> float:
    """The plant transfer of `plants/transfer.reference_power_mw`, operation for operation."""
    start, end, power_kw = native
    dc_capacity_kw = configuration.dc_capacity_kw
    if dc_capacity_kw is None:
        raise ValueError("Tracker geometry needs the reference DC capacity")
    power_mw = power_kw / configuration.capacity_kw * configuration.dc_capacity_mw
    loss_free = power_kw / dc_capacity_kw / (1 - configuration.losses_percent / 100)
    return power_mw * tracking.interval_gain(
        start, end, loss_free, configuration.latitude, configuration.longitude, model=version
    )


def _covers(hours: list[datetime], windows: Windows) -> bool:
    for begin, finish in windows:
        cursor = begin
        for hour in hours:
            if hour + HOUR <= cursor or hour >= finish:
                continue
            if hour > cursor:
                return False
            cursor = hour + HOUR
        if cursor < finish:
            return False
    return bool(windows)


def _hours(
    connection: Connection, configuration: Configuration, skipped: Counter[str]
) -> tuple[dict[date, list[_Hour]], dict[date, Windows]]:
    production = _production(connection, configuration, skipped)
    zone = ZoneInfo(configuration.timezone)
    windows: dict[date, Windows] = {}
    needed: dict[date, list[datetime]] = defaultdict(list)
    for hour in sorted(production):
        day = hour.astimezone(zone).date()
        if day not in windows:
            windows[day] = daylight_window(
                day, configuration.timezone, configuration.latitude, configuration.longitude
            ).windows
        if any(hour < finish and hour + HOUR > begin for begin, finish in windows[day]):
            needed[day].append(hour)
        else:
            skipped["outside daylight"] += 1
    references = _references(
        connection, configuration, {hour for hours in needed.values() for hour in hours}, skipped
    )
    complete: dict[date, list[_Hour]] = {}
    for day, hours in sorted(needed.items()):
        usable = [hour for hour in hours if hour in references]
        skipped["reference missing"] += len(hours) - len(usable)
        if not _covers(usable, windows[day]):
            skipped["incomplete day"] += len(usable)
            continue
        complete[day] = [
            _Hour(
                hour,
                production[hour],
                {
                    version: tuple(
                        PowerInterval(
                            native[0], native[1], transferred_mw(configuration, native, version)
                        )
                        for native in references[hour]
                    )
                    for version in VERSIONS
                },
            )
            for hour in usable
        ]
    return complete, windows


def _estimate_mwh(hour: _Hour, version: str, factor: float, limit_mw: float | None) -> float:
    total = 0.0
    for piece in hour.pieces[version]:
        hours = (
            min(piece.end, hour.start + HOUR) - max(piece.start, hour.start)
        ).total_seconds() / 3600
        power = (piece.power_mw or 0.0) * factor
        total += (power if limit_mw is None else min(limit_mw, power)) * hours
    return total


def _fit(
    configuration: Configuration,
    days: dict[date, list[_Hour]],
    windows: dict[date, Windows],
    version: str,
) -> float | None:
    result = fit_calibration(
        tuple(
            CalibrationSample(hour.start, hour.start + HOUR, hour.energy_mwh, hour.pieces[version])
            for hours in days.values()
            for hour in hours
        ),
        timezone=configuration.timezone,
        normalization_mw=configuration.dc_capacity_mw,
        limit_mw=configuration.limit_mw,
        required_windows={day: windows[day] for day in days},
    )
    factor = result.report.get("fitted_factor")
    return factor if isinstance(factor, float) else None


def _factors(
    configuration: Configuration,
    days: dict[date, list[_Hour]],
    windows: dict[date, Windows],
    report: PlantReport,
    hours: int,
) -> dict[str, float] | None:
    factors = {}
    for version in VERSIONS:
        try:
            factor = _fit(configuration, days, windows, version)
        except Exception as error:
            report.skipped[f"fit failed: {type(error).__name__}"] += hours
            return None
        if factor is None:
            report.skipped["fit insufficient"] += hours
            return None
        factors[version] = factor
    return factors


def _score(
    configuration: Configuration,
    complete: dict[date, list[_Hour]],
    windows: dict[date, Windows],
    report: PlantReport,
) -> None:
    """Score each week after the first with factors refitted on the days before it."""
    report.complete_days += len(complete)
    if not complete:
        return
    first = min(complete)
    weeks: dict[int, list[date]] = defaultdict(list)
    for day in complete:
        weeks[(day - first).days // WEEK_DAYS].append(day)
    for week, days in sorted(weeks.items()):
        hours = [hour for day in days for hour in complete[day]]
        if week == 0:
            report.skipped["fit-only first week"] += len(hours)
            continue
        start = first + timedelta(days=week * WEEK_DAYS)
        fit_days = {
            day: complete[day]
            for day in complete
            if start - timedelta(days=FIT_WINDOW_DAYS) <= day < start
        }
        factors = _factors(configuration, fit_days, windows, report, len(hours))
        if factors is None:
            report.skipped_weeks += 1
            continue
        squared = dict.fromkeys(VERSIONS, 0.0)
        for hour in hours:
            for version in VERSIONS:
                estimate = _estimate_mwh(hour, version, factors[version], configuration.limit_mw)
                error = estimate - hour.energy_mwh
                report.errors[version].add(error, configuration.dc_capacity_mw)
                squared[version] += error * error
        for version in VERSIONS:
            report.factors[version].append(factors[version])
        report.weeks.append(
            WeekErrors(squared[tracking.MODEL_V1], squared[tracking.MODEL_V2], len(hours))
        )
        report.evaluated_days += len(days)


def _evaluate_plant(
    connection: Connection, ordinal: int, configurations: list[Configuration]
) -> PlantReport:
    report = PlantReport(ordinal)
    modeled = [configuration for configuration in configurations if configuration.modeled]
    if not modeled:
        report.status = "geometry not modeled"
    for configuration in modeled:
        complete, windows = _hours(connection, configuration, report.skipped)
        _score(configuration, complete, windows, report)
    return report


def evaluate(connection: Connection) -> list[PlantReport]:
    reports = []
    for ordinal, configurations in enumerate(_configurations(connection), start=1):
        try:
            reports.append(_evaluate_plant(connection, ordinal, configurations))
        except Exception as error:
            connection.rollback()
            reports.append(PlantReport(ordinal, status=f"failed: {type(error).__name__}"))
    return reports


def rmse_difference(weeks: Sequence[WeekErrors]) -> tuple[float, float, float]:
    """RMSE(v2) - RMSE(v1) over all intervals, with a delete-one-week jackknife interval."""
    first = sum(week.squared_v1 for week in weeks)
    second = sum(week.squared_v2 for week in weeks)
    count = sum(week.count for week in weeks)
    difference = sqrt(second / count) - sqrt(first / count)
    left_out = [
        sqrt(max(second - week.squared_v2, 0.0) / (count - week.count))
        - sqrt(max(first - week.squared_v1, 0.0) / (count - week.count))
        for week in weeks
    ]
    mean = fmean(left_out)
    size = len(weeks)
    spread = sqrt((size - 1) / size * sum((value - mean) ** 2 for value in left_out))
    margin = NormalDist().inv_cdf(0.5 + CONFIDENCE / 2) * spread
    return difference, difference - margin, difference + margin


def _metrics(errors: Errors) -> str:
    count = errors.count
    return (
        f"MAE {errors.absolute / count:.4f} MWh ({100 * errors.share_absolute / count:.3f} %), "
        f"RMSE {sqrt(errors.squared / count):.4f} MWh "
        f"({100 * sqrt(errors.share_squared / count):.3f} %), "
        f"bias {errors.signed / count:.4f} MWh ({100 * errors.share_signed / count:.3f} %)"
    )


def _plant(report: PlantReport) -> list[str]:
    if report.status != "evaluated":
        return [f"plant {report.ordinal}: {report.status}"]
    evaluated = report.errors[tracking.MODEL_V1].count
    skipped = ", ".join(f"{reason} {count}" for reason, count in sorted((+report.skipped).items()))
    lines = [
        f"plant {report.ordinal}",
        f"  days: complete {report.complete_days}, evaluated {report.evaluated_days}; "
        f"weeks: scored {len(report.weeks)}, skipped {report.skipped_weeks}",
        f"  intervals: evaluated {evaluated}",
        f"  skipped: {skipped or 'none'}",
    ]
    if evaluated:
        lines.extend(
            f"  {LABELS[version]}: {_metrics(report.errors[version])}, "
            f"median factor {median(report.factors[version]):.4f}"
            for version in VERSIONS
        )
    difference = report.difference
    if difference is not None:
        lines.append(
            f"  RMSE v2 - v1: {difference[0]:.4f} MWh, 95 % interval "
            f"[{difference[1]:.4f}, {difference[2]:.4f}]"
        )
    lines.append(f"  decision: {'yes' if report.not_worse else 'no'}")
    return lines


def render(reports: Sequence[PlantReport]) -> str:
    lines = ["tracker geometry evaluation (read-only)", RULE]
    for report in reports:
        lines.extend(_plant(report))
    if not reports:
        lines.append("no tracking plant")
    decided = [report for report in reports if report.status != "geometry not modeled"]
    verdict = bool(decided) and all(report.not_worse for report in decided)
    lines.append(f"v2 not worse: {'yes' if verdict else 'no'}")
    return "\n".join(lines) + "\n"


def main(environ: Mapping[str, str] | None = None) -> int:
    settings = os.environ if environ is None else environ
    try:
        connection = connect(settings)
        try:
            reports = evaluate(connection)
        finally:
            connection.close()
    except MissingSettingError as error:
        print(f"missing environment variable {error.name}")
        return 2
    except Exception as error:
        print(f"evaluation failed: {type(error).__name__}")
        return 1
    print(render(reports), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
