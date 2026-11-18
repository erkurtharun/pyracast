from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

type Sunlight = Literal["day", "night", "partial", "unknown"]
SUNLIGHT = ("day", "night", "partial", "unknown")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Normalized interval timestamps must be ISO strings")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("Normalized interval timestamps must carry a UTC offset")
    return result


@dataclass(frozen=True)
class NormalizedInterval:
    """One provider power interval: mean power in kW, None where the provider gave none. A
    clipped interval keeps the provider's bounds in `original_start` and `original_end`."""

    start: datetime
    end: datetime
    power_kw: float | None
    sunlight: Sunlight = "unknown"
    original_start: datetime | None = None
    original_end: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> NormalizedInterval:
        start, end = _timestamp(row.get("start")), _timestamp(row.get("end"))
        power, sunlight = row.get("power_kw"), row.get("sunlight", "unknown")
        if power is not None and (isinstance(power, bool) or not isinstance(power, int | float)):
            raise ValueError("Normalized power must be numeric or missing")
        if sunlight not in SUNLIGHT:
            raise ValueError("Invalid normalized sunlight classification")
        original_start, original_end = (
            None if row.get(key) is None else _timestamp(row.get(key))
            for key in ("original_start", "original_end")
        )
        return cls(start, end, power, cast(Sunlight, sunlight), original_start, original_end)

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, object]]) -> tuple[NormalizedInterval, ...]:
        return tuple(cls.from_row(row) for row in rows)

    def to_row(self) -> dict[str, object]:
        row: dict[str, object] = {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "power_kw": self.power_kw,
            "sunlight": self.sunlight,
        }
        if self.original_start is not None and self.original_end is not None:
            row |= {
                "original_start": self.original_start.isoformat(),
                "original_end": self.original_end.isoformat(),
            }
        return row
