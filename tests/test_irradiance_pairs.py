import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pyracast.irradiance.pairs import load_pairs

START = datetime(2026, 1, 1, tzinfo=UTC)


def power(end: datetime = START + timedelta(minutes=30), **updates: Any) -> dict[str, Any]:
    return {"period_end": end.isoformat(), "period": "PT30M", "pv_power_rooftop": 0} | updates


def weather(index: int, **updates: Any) -> dict[str, Any]:
    return {
        "period_end": (START + timedelta(minutes=5 * (index + 1))).isoformat(),
        "period": "PT5M",
        "ghi": index * 100,
        "air_temp": -5 + index,
        "snow_soiling_rooftop": index * 10,
    } | updates


def files(tmp_path: Path, powers: object, weathers: object) -> tuple[Path, Path]:
    p, w = tmp_path / "p.json", tmp_path / "w.json"
    p.write_text(json.dumps({"estimated_actuals": powers}), encoding="utf-8")
    w.write_text(json.dumps({"estimated_actuals": weathers}), encoding="utf-8")
    return p, w


def test_complete_utc_alignment_weighted_means_and_source_hashes(tmp_path: Path) -> None:
    rows = [weather(i) for i in range(6)]
    rows[0]["period_end"] = "2026-01-01T03:05:00+03:00"
    p, w = files(tmp_path, [power()], rows)
    dataset = load_pairs(p, w)
    assert dataset.power_sha256 == hashlib.sha256(p.read_bytes()).hexdigest()
    assert dataset.weather_sha256 == hashlib.sha256(w.read_bytes()).hexdigest()
    assert (dataset.unmatched_power_intervals, dataset.unmatched_weather_intervals) == (0, 0)
    (sample,) = dataset.samples
    assert (sample.start, sample.end, sample.power_kw) == (START, START + timedelta(minutes=30), 0)
    assert (sample.ghi_wm2, sample.air_temp_c, sample.snow_loss_fraction) == (250, -2.5, 0.25)
    assert len(sample.weather) == 6
    assert sample.weather[0].ghi_wm2 == 0
    assert sample.weather[0].end == START + timedelta(minutes=5)


def test_gaps_and_unmatched_edges_are_counted(tmp_path: Path) -> None:
    powers = [power(), power(START + timedelta(minutes=60)), power(START + timedelta(minutes=90))]
    rows = [
        weather(i, ghi=max(i, 0) * 100, snow_soiling_rooftop=min(max(i, 0) * 10, 100))
        for i in range(-1, 19)
        if i != 8
    ]
    p, w = files(tmp_path, powers, rows)
    result = load_pairs(p, w)
    assert len(result.samples) == 2
    assert (result.unmatched_power_intervals, result.unmatched_weather_intervals) == (1, 7)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pv_power_rooftop", -1),
        ("pv_power_rooftop", True),
        ("pv_power_rooftop", None),
        ("pv_power_rooftop", float("nan")),
        ("pv_power_rooftop", float("inf")),
        ("pv_power_rooftop", 10**400),
        ("period", "PT5M"),
        ("period_end", "2026-01-01T00:30:00"),
        ("period_end", "invalid"),
        ("period_end", None),
    ],
)
def test_rejects_invalid_power_fields(tmp_path: Path, field: str, value: object) -> None:
    p, w = files(tmp_path, [power() | {field: value}], [])
    with pytest.raises(ValueError, match=r"Invalid|sorted|interval objects"):
        load_pairs(p, w)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ghi", -1),
        ("ghi", None),
        ("air_temp", float("nan")),
        ("air_temp", True),
        ("snow_soiling_rooftop", -1),
        ("snow_soiling_rooftop", 101),
        ("period", "PT30M"),
    ],
)
def test_rejects_invalid_weather_fields(tmp_path: Path, field: str, value: object) -> None:
    p, w = files(tmp_path, [], [weather(0, **{field: value})])
    with pytest.raises(ValueError, match=r"Invalid|sorted|interval objects"):
        load_pairs(p, w)


@pytest.mark.parametrize(
    ("powers", "weathers"),
    [
        ([power(), power()], []),
        ([power(START + timedelta(minutes=60)), power()], []),
        ([], [weather(0), weather(0)]),
        ([], [weather(1), weather(0)]),
        ([None], []),
    ],
)
def test_rejects_overlapping_unsorted_or_non_object_rows(
    tmp_path: Path, powers: object, weathers: object
) -> None:
    p, w = files(tmp_path, powers, weathers)
    with pytest.raises(ValueError, match=r"Invalid|sorted|interval objects"):
        load_pairs(p, w)


def test_rejects_missing_array(tmp_path: Path) -> None:
    p, w = files(tmp_path, [], [])
    p.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="estimated_actuals"):
        load_pairs(p, w)
