from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pyracast.plants.numerics import PowerInterval, forecast
from pyracast.plants.operating_limits import OperatingLimit, affected_power


def test_partial_outage_splits_native_power_before_hourly_clipping() -> None:
    start = datetime(2026, 11, 1, 10, tzinfo=UTC)
    half = start + timedelta(minutes=30)
    end = start + timedelta(hours=1)
    pieces = affected_power(
        (PowerInterval(start, half, 80), PowerInterval(half, end, 120)),
        (OperatingLimit(start + timedelta(minutes=15), end - timedelta(minutes=15), 65, 30, 20),),
        dc_capacity_mw=130,
        normal_limit_mw=100,
        factor=1,
        net_export=False,
    )
    result = forecast(pieces, start=start, end=end, resolution_minutes=60, limit_mw=None)
    assert result["energy_mwh"] == 60
    assert [piece.power_mw for piece in pieces] == [80, 30, 30, 100]
    assert result["complete"] is True


@pytest.mark.parametrize("caps", [(0, None, None), (None, 0, None), (None, None, 0)])
def test_known_zero_capacity_proves_zero_even_without_weather(
    caps: tuple[float | None, float | None, float | None],
) -> None:
    start = datetime(2026, 11, 1, 10, tzinfo=UTC)
    end = start + timedelta(hours=1)
    pieces = affected_power(
        (PowerInterval(start, end, None),),
        (OperatingLimit(start, None, *caps),),
        dc_capacity_mw=130,
        normal_limit_mw=None,
        factor=0.9,
        net_export=True,
    )
    assert pieces == (PowerInterval(start, end, 0),)


def test_unknown_impact_is_missing_and_export_limit_only_caps_net_export() -> None:
    start = datetime(2026, 11, 1, 10, tzinfo=UTC)
    end = start + timedelta(hours=1)
    values: dict[str, Any] = dict(dc_capacity_mw=130, normal_limit_mw=None, factor=0.9)
    source = (PowerInterval(start, end, 120),)
    unknown = (OperatingLimit(start, end, None, None, None),)
    assert affected_power(source, unknown, net_export=True, **values)[0].power_mw is None
    limited = (OperatingLimit(start, None, None, None, 40),)
    assert affected_power(source, limited, net_export=True, **values)[0].power_mw == 40
    assert affected_power(source, limited, net_export=False, **values)[0].power_mw == 108
    assert (
        affected_power((PowerInterval(start, end, None),), (), net_export=True, **values)[
            0
        ].power_mw
        is None
    )
