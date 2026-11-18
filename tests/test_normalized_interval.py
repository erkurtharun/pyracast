from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pyracast.intervals import NormalizedInterval
from pyracast.providers import clip_provider_data, normalize_payload

START = datetime(2026, 11, 9, 10, tzinfo=UTC)
END = START + timedelta(minutes=30)
ROW: dict[str, Any] = {
    "start": START.isoformat(),
    "end": END.isoformat(),
    "power_kw": 0.5,
    "sunlight": "day",
}


@pytest.mark.parametrize(
    "row",
    [
        ROW,
        {**ROW, "power_kw": None},
        {**ROW, "power_kw": 3},
        {
            **ROW,
            "start": (START + timedelta(minutes=10)).isoformat(),
            "original_start": START.isoformat(),
            "original_end": END.isoformat(),
        },
    ],
)
def test_a_stored_interval_row_round_trips(row: dict[str, Any]) -> None:
    assert NormalizedInterval.from_row(row).to_row() == row


def test_a_stored_row_without_sunlight_reads_as_unknown() -> None:
    row = {key: value for key, value in ROW.items() if key != "sunlight"}
    assert NormalizedInterval.from_row(row) == NormalizedInterval(START, END, 0.5)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"start": None}, "timestamps must be ISO strings"),
        ({"end": 5}, "timestamps must be ISO strings"),
        ({"power_kw": "1"}, "power must be numeric"),
        ({"power_kw": True}, "power must be numeric"),
        ({"sunlight": "dusk"}, "sunlight classification"),
        ({"original_start": 1}, "timestamps must be ISO strings"),
    ],
)
def test_a_stored_row_of_another_shape_is_refused(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        NormalizedInterval.from_row({**ROW, **change})


def test_provider_data_carries_typed_intervals_through_a_clip() -> None:
    data = normalize_payload(
        {"forecasts": [{"period_end": END.isoformat(), "period": "PT30M", "pv_estimate": 1.5}]}
    )
    assert data.intervals == (NormalizedInterval(START, END, 1.5),)

    clipped = clip_provider_data(data, START + timedelta(minutes=10), END)

    assert clipped.intervals == (
        NormalizedInterval(START + timedelta(minutes=10), END, 1.5, "unknown", START, END),
    )
