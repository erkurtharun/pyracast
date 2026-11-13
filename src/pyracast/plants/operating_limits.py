from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from pyracast.plants.numerics import PowerInterval


@dataclass(frozen=True)
class OperatingLimit:
    start: datetime
    end: datetime | None
    dc_mw: float | None
    inverter_mw: float | None
    export_mw: float | None


def affected_power(
    intervals: tuple[PowerInterval, ...],
    limits: tuple[OperatingLimit, ...],
    *,
    dc_capacity_mw: float,
    normal_limit_mw: float | None,
    factor: float,
    net_export: bool,
) -> tuple[PowerInterval, ...]:
    result = []
    for interval in intervals:
        boundaries = {interval.start, interval.end}
        for limit in limits:
            for boundary in (limit.start, limit.end):
                if boundary is not None and interval.start < boundary < interval.end:
                    boundaries.add(boundary)
        ordered = sorted(boundaries)
        for start, end in pairwise(ordered):
            active = next(
                (
                    limit
                    for limit in limits
                    if limit.start < end and (limit.end is None or limit.end > start)
                ),
                None,
            )
            power = interval.power_mw
            caps = [] if normal_limit_mw is None else [normal_limit_mw]
            if active is not None:
                if all(
                    value is None for value in (active.dc_mw, active.inverter_mw, active.export_mw)
                ):
                    power = None
                if power is not None and active.dc_mw is not None:
                    power *= min(active.dc_mw / dc_capacity_mw, 1)
                if active.inverter_mw is not None:
                    caps.append(active.inverter_mw)
                if net_export and active.export_mw is not None:
                    caps.append(active.export_mw)
                if active.dc_mw == 0 or 0 in caps:
                    power = 0.0
            if power is not None:
                power *= factor
                if caps:
                    power = min(power, *caps)
            result.append(PowerInterval(start, end, power))
    return tuple(result)
