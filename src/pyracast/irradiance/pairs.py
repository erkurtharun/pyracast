import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import pairwise
from math import fsum, isfinite
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WeatherInterval:
    start: datetime
    end: datetime
    ghi_wm2: float
    air_temp_c: float
    snow_loss_fraction: float


@dataclass(frozen=True)
class PairedInterval:
    start: datetime
    end: datetime
    power_kw: float
    ghi_wm2: float
    air_temp_c: float
    snow_loss_fraction: float
    weather: tuple[WeatherInterval, ...]


@dataclass(frozen=True)
class PairedDataset:
    samples: tuple[PairedInterval, ...]
    unmatched_power_intervals: int
    unmatched_weather_intervals: int
    power_sha256: str
    weather_sha256: str


@dataclass(frozen=True)
class _PowerInterval:
    start: datetime
    end: datetime
    power_kw: float


def _number(
    row: dict[str, Any], key: str, *, minimum: float | None = None, maximum: float | None = None
) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"Invalid {key}")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"Invalid {key}") from exc
    if (
        not isfinite(result)
        or (minimum is not None and result < minimum)
        or (maximum is not None and result > maximum)
    ):
        raise ValueError(f"Invalid {key}")
    return result


def _bounds(row: dict[str, Any], period: str, duration: timedelta) -> tuple[datetime, datetime]:
    if row.get("period") != period or not isinstance(row.get("period_end"), str):
        raise ValueError("Invalid period or period_end")
    try:
        end = datetime.fromisoformat(row["period_end"])
        if end.utcoffset() is None:
            raise ValueError("Timestamp must be timezone-aware")
        end = end.astimezone(UTC)
        return end - duration, end
    except (OverflowError, ValueError) as exc:
        raise ValueError("Invalid period_end") from exc


def _load(path: Path) -> tuple[list[dict[str, Any]], str]:
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("estimated_actuals"), list):
        raise ValueError("Expected an estimated_actuals array")
    rows = data["estimated_actuals"]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("Expected interval objects")
    return rows, sha256(raw).hexdigest()


def _power(rows: list[dict[str, Any]]) -> list[_PowerInterval]:
    result: list[_PowerInterval] = []
    for row in rows:
        start, end = _bounds(row, "PT30M", timedelta(minutes=30))
        if result and start < result[-1].end:
            raise ValueError("Power intervals must be sorted and non-overlapping")
        result.append(_PowerInterval(start, end, _number(row, "pv_power_rooftop", minimum=0)))
    return result


def _weather(rows: list[dict[str, Any]]) -> list[WeatherInterval]:
    result: list[WeatherInterval] = []
    for row in rows:
        start, end = _bounds(row, "PT5M", timedelta(minutes=5))
        if result and start < result[-1].end:
            raise ValueError("Weather intervals must be sorted and non-overlapping")
        result.append(
            WeatherInterval(
                start,
                end,
                _number(row, "ghi", minimum=0),
                _number(row, "air_temp"),
                _number(row, "snow_soiling_rooftop", minimum=0, maximum=100) / 100,
            )
        )
    return result


def load_pairs(power_path: Path, weather_path: Path) -> PairedDataset:
    power_rows, power_hash = _load(power_path)
    weather_rows, weather_hash = _load(weather_path)
    powers = _power(power_rows)
    weather = _weather(weather_rows)
    samples: list[PairedInterval] = []
    cursor = 0
    consumed = 0
    for power in powers:
        while cursor < len(weather) and weather[cursor].end <= power.start:
            cursor += 1
        selected = weather[cursor : cursor + 6]
        if (
            len(selected) != 6
            or selected[0].start != power.start
            or selected[-1].end != power.end
            or any(a.end != b.start for a, b in pairwise(selected))
        ):
            continue
        seconds = (power.end - power.start).total_seconds()

        def mean(
            attribute: str, rows: list[WeatherInterval] = selected, duration: float = seconds
        ) -> float:
            return (
                fsum(getattr(w, attribute) * (w.end - w.start).total_seconds() for w in rows)
                / duration
            )

        samples.append(
            PairedInterval(
                power.start,
                power.end,
                power.power_kw,
                mean("ghi_wm2"),
                mean("air_temp_c"),
                mean("snow_loss_fraction"),
                tuple(selected),
            )
        )
        consumed += 6
        cursor += 6
    return PairedDataset(
        tuple(samples),
        len(powers) - len(samples),
        len(weather) - consumed,
        power_hash,
        weather_hash,
    )
